param(
    [string]$InstallerPath = "",
    [string]$InstallDirectory = "$env:LOCALAPPDATA\WGSExtract",
    [string]$WorkspaceDirectory = "$env:LOCALAPPDATA\dev.guiforcli.web.embed.wgsextract\BundleWorkspaces\wgs-extract",
    [string]$LogDirectory = "tmp\tauri-lifecycle",
    [int]$InstallTimeoutSeconds = 180,
    [int]$StartupTimeoutSeconds = 60,
    [int]$SetupTimeoutSeconds = 1800,
    [int]$ShutdownTimeoutSeconds = 30,
    [switch]$SkipSetup,
    [switch]$AutomatedAdmin,
    [switch]$PrepareAdminBroker,
    [switch]$RemoveAdminBroker,
    [switch]$DriveUI,
    [string]$UIPages = "settings,fastq,library,extract",
    [int]$UISetupTimeoutSeconds = 1800,
    [string]$AdminTaskName = "GUIForCLIWindowsAdminBroker",
    [string]$AdminQueueDirectory = "tmp\windows-admin-broker",
    [switch]$KeepInstalled
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

$script:StartedAt = Get-Date
$script:StageResults = [System.Collections.Generic.List[object]]::new()
$LogDirectory = (New-Item -ItemType Directory -Force -Path $LogDirectory).FullName
$PortFile = Join-Path $LogDirectory "installed-tauri-port.txt"
# Written by the app next to the port file (only because GFC_PORT_FILE is set): the per-launch API token.
$TokenFile = "$PortFile.token"
$SetupLog = Join-Path $LogDirectory "installed-setup.ndjson"
$SetupSummary = Join-Path $LogDirectory "installed-setup-summary.json"
$UninstallLog = Join-Path $LogDirectory "installed-uninstall.ndjson"
$UninstallSummary = Join-Path $LogDirectory "installed-uninstall-summary.json"
$WebViewDataDirectory = "$env:LOCALAPPDATA\dev.guiforcli.web.embed.wgsextract"
$script:AdminBroker = $null

function Resolve-InstallerPath {
    if ($InstallerPath) {
        return (Resolve-Path $InstallerPath).Path
    }

    $candidates = @(
        Get-ChildItem -LiteralPath "out\release\tauri" -Filter "*-setup.exe" -File -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath "app\web\packagers\tauri\target\release\bundle\nsis" -Filter "*-setup.exe" -File -ErrorAction SilentlyContinue
    )
    $candidates = @($candidates | Sort-Object LastWriteTimeUtc -Descending)
    if ($candidates.Count -eq 0) {
        throw "No Tauri NSIS installer was found under out\release\tauri or the Tauri target bundle directory."
    }
    return $candidates[0].FullName
}

function Write-Stage {
    param([string]$Name, [string]$Status = "start", [string]$Detail = "")

    $elapsed = [math]::Round(((Get-Date) - $script:StartedAt).TotalSeconds, 1)
    $message = "[$elapsed s] [$Status] $Name"
    if ($Detail) {
        $message = "$message - $Detail"
    }
    Write-Host $message
}

function Complete-Stage {
    param([string]$Name, [datetime]$Started, [string]$Detail = "")

    $duration = [math]::Round(((Get-Date) - $Started).TotalSeconds, 1)
    $script:StageResults.Add([ordered]@{ name = $Name; status = "ok"; seconds = $duration; detail = $Detail })
    Write-Stage -Name $Name -Status "ok" -Detail "$duration s $Detail"
}

function Fail-Stage {
    param([string]$Name, [datetime]$Started, [string]$Detail)

    $duration = [math]::Round(((Get-Date) - $Started).TotalSeconds, 1)
    $script:StageResults.Add([ordered]@{ name = $Name; status = "failed"; seconds = $duration; detail = $Detail })
    Write-Stage -Name $Name -Status "failed" -Detail $Detail
    Write-Summary
    throw $Detail
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $utf8NoBom)
}

function Test-RunningElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Grant-ScheduledTaskRunAccess {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $task = $service.GetFolder("\").GetTask($TaskName)
    $sddl = $task.GetSecurityDescriptor(0xF)
    $builtinUsersExecuteAce = "(A;;GRGX;;;BU)"
    $sddl = $sddl.Replace($builtinUsersExecuteAce, "")
    $currentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $currentUserExecuteAce = "(A;;GRGX;;;$currentUserSid)"
    if ($sddl -like "*$currentUserExecuteAce*") {
        return
    }

    $saclIndex = $sddl.IndexOf("S:")
    if ($saclIndex -ge 0) {
        $updated = $sddl.Substring(0, $saclIndex) + $currentUserExecuteAce + $sddl.Substring($saclIndex)
    } else {
        $updated = $sddl + $currentUserExecuteAce
    }
    $task.SetSecurityDescriptor($updated, 0)
}

function Ensure-WindowsAdminBroker {
    $queuePath = (New-Item -ItemType Directory -Force -Path $AdminQueueDirectory).FullName
    $brokerScript = (Resolve-Path (Join-Path $PSScriptRoot "windows-admin-task-broker.ps1")).Path
    $taskArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$brokerScript`" -QueueDirectory `"$queuePath`""
    $existing = Get-ScheduledTask -TaskName $AdminTaskName -ErrorAction SilentlyContinue

    if ($existing) {
        $action = @($existing.Actions)[0]
        $matchesExpectedAction = $action.Execute -like "*powershell.exe" -and $action.Arguments -eq $taskArguments
        if (-not $matchesExpectedAction) {
            if (-not (Test-RunningElevated)) {
                throw "Automated admin broker task '$AdminTaskName' exists but does not match this checkout. Re-run -PrepareAdminBroker from an elevated shell."
            }
            Unregister-ScheduledTask -TaskName $AdminTaskName -Confirm:$false
        } else {
            if (Test-RunningElevated) {
                Grant-ScheduledTaskRunAccess -TaskName $AdminTaskName
            }
            return [ordered]@{ taskName = $AdminTaskName; queueDirectory = $queuePath; created = $false }
        }
    }

    if (-not (Test-RunningElevated)) {
        throw "Automated admin broker task '$AdminTaskName' is not installed. Run once from an elevated shell: pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\validate-windows-tauri-lifecycle.ps1 -PrepareAdminBroker -AdminTaskName '$AdminTaskName' -AdminQueueDirectory '$AdminQueueDirectory'"
    }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArguments
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances Parallel -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $AdminTaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
    Grant-ScheduledTaskRunAccess -TaskName $AdminTaskName
    return [ordered]@{ taskName = $AdminTaskName; queueDirectory = $queuePath; created = $true }
}

function Remove-WindowsAdminBroker {
    if (-not (Test-RunningElevated)) {
        throw "Removing admin broker task '$AdminTaskName' requires an elevated shell."
    }
    Unregister-ScheduledTask -TaskName $AdminTaskName -Confirm:$false -ErrorAction SilentlyContinue
}

function Write-Summary {
    $summaryPath = Join-Path $LogDirectory "summary.json"
    $summary = [ordered]@{
        startedAt = $script:StartedAt.ToString("o")
        completedAt = (Get-Date).ToString("o")
        stages = $script:StageResults
        installDirectory = $InstallDirectory
        workspaceDirectory = $WorkspaceDirectory
        setupLog = $SetupLog
        setupSummary = $SetupSummary
        uninstallLog = $UninstallLog
        uninstallSummary = $UninstallSummary
    }
    Write-Utf8File -LiteralPath $summaryPath -Value (($summary | ConvertTo-Json -Depth 8) + "`n")
    Write-Host "Summary: $summaryPath"
}

function Invoke-Stage {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$ScriptBlock
    )

    $started = Get-Date
    Write-Stage -Name $Name
    try {
        $detail = & $ScriptBlock
        Complete-Stage -Name $Name -Started $started -Detail ([string]$detail)
    }
    catch {
        Fail-Stage -Name $Name -Started $started -Detail $_.Exception.Message
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 120,
        [string]$WorkingDirectory = (Get-Location).Path,
        [hashtable]$Environment = @{},
        [string]$Name = (Split-Path -Leaf $FilePath)
    )

    $stdoutPath = Join-Path $LogDirectory "$($Name -replace '[^A-Za-z0-9_.-]', '_').stdout.log"
    $stderrPath = Join-Path $LogDirectory "$($Name -replace '[^A-Za-z0-9_.-]', '_').stderr.log"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -ErrorAction SilentlyContinue

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    foreach ($argument in $Arguments) {
        [void]$psi.ArgumentList.Add($argument)
    }
    foreach ($key in $Environment.Keys) {
        $psi.Environment[$key] = [string]$Environment[$key]
    }

    # Touch both log files so external watchers/tails see them immediately.
    [System.IO.File]::WriteAllText($stdoutPath, "")
    [System.IO.File]::WriteAllText($stderrPath, "")
    $stdoutWriter = [System.IO.StreamWriter]::new($stdoutPath, $true, [System.Text.UTF8Encoding]::new($false))
    $stdoutWriter.AutoFlush = $true
    $stderrWriter = [System.IO.StreamWriter]::new($stderrPath, $true, [System.Text.UTF8Encoding]::new($false))
    $stderrWriter.AutoFlush = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.EnableRaisingEvents = $true

    $script:__externalState = @{ writer = $stdoutWriter; err = $stderrWriter; name = $Name }
    $stdoutHandler = {
        param($sender, $eventArgs)
        if ($null -ne $eventArgs.Data) {
            $state = $script:__externalState
            try { $state.writer.WriteLine($eventArgs.Data) } catch { }
            [Console]::Out.WriteLine("[$($state.name)|stdout] $($eventArgs.Data)")
        }
    }
    $stderrHandler = {
        param($sender, $eventArgs)
        if ($null -ne $eventArgs.Data) {
            $state = $script:__externalState
            try { $state.err.WriteLine($eventArgs.Data) } catch { }
            [Console]::Out.WriteLine("[$($state.name)|stderr] $($eventArgs.Data)")
        }
    }
    $stdoutEvent = Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -Action $stdoutHandler
    $stderrEvent = Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -Action $stderrHandler
    try {
        [void]$process.Start()
        $process.BeginOutputReadLine()
        $process.BeginErrorReadLine()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw "$Name timed out after $TimeoutSeconds seconds."
        }
        # Drain any remaining buffered output before tearing down handlers.
        $process.WaitForExit()
    }
    finally {
        Unregister-Event -SourceIdentifier $stdoutEvent.Name -ErrorAction SilentlyContinue
        Unregister-Event -SourceIdentifier $stderrEvent.Name -ErrorAction SilentlyContinue
        $stdoutWriter.Dispose()
        $stderrWriter.Dispose()
    }
    if ($process.ExitCode -ne 0) {
        throw "$Name exited $($process.ExitCode). stdout=$stdoutPath stderr=$stderrPath"
    }
    return [ordered]@{ stdout = $stdoutPath; stderr = $stderrPath; exitCode = $process.ExitCode }
}

function Get-SelfAncestry {
    $ids = New-Object System.Collections.Generic.HashSet[int]
    [void]$ids.Add($PID)
    $procMap = @{}
    foreach ($p in Get-CimInstance Win32_Process) { $procMap[[int]$p.ProcessId] = [int]$p.ParentProcessId }
    $cursor = $PID
    while ($procMap.ContainsKey($cursor)) {
        $parent = $procMap[$cursor]
        if (-not $parent -or $parent -eq $cursor -or $ids.Contains($parent)) { break }
        [void]$ids.Add($parent)
        $cursor = $parent
    }
    return $ids
}

function Get-TargetProcesses {
    $skip = Get-SelfAncestry
    Get-CimInstance Win32_Process | Where-Object {
        -not $skip.Contains([int]$_.ProcessId) -and
        $_.CommandLine -and (
            $_.CommandLine -like "*WGSExtract*" -or
            $_.CommandLine -like "*gui-for-cli-webui-tauri*" -or
            $_.CommandLine -like "*BundleWorkspaces\\wgs-extract*" -or
            $_.CommandLine -like "*BundleWorkspaces/wgs-extract*"
        )
    } | Select-Object ProcessId, ParentProcessId, Name, CommandLine
}

function Stop-TargetProcesses {
    $processes = @(Get-TargetProcesses)
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $remaining = @(Get-TargetProcesses)
    if ($remaining.Count -gt 0) {
        throw "Target processes still running: $($remaining.ProcessId -join ', ')"
    }
    return "stopped $($processes.Count) process(es)"
}

function Assert-Exists {
    param([string[]]$Paths)

    $missing = @($Paths | Where-Object { -not (Test-Path -LiteralPath $_) })
    if ($missing.Count -gt 0) {
        throw "Missing expected path(s): $($missing -join '; ')"
    }
}

function Assert-NotExists {
    param([string[]]$Paths)

    $present = @($Paths | Where-Object { Test-Path -LiteralPath $_ })
    if ($present.Count -gt 0) {
        throw "Unexpected remaining path(s): $($present -join '; ')"
    }
}

function Remove-Tree {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    for ($attempt = 1; $attempt -le 5; $attempt += 1) {
        if (-not (Test-Path -LiteralPath $LiteralPath)) {
            return
        }
        try {
            Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if (-not (Test-Path -LiteralPath $LiteralPath)) {
                return
            }
            if ($attempt -eq 5) {
                throw
            }
            Start-Sleep -Milliseconds (250 * $attempt)
        }
    }
}

function Install-App {
    $resolvedInstaller = Resolve-InstallerPath
    Invoke-External -FilePath $resolvedInstaller -Arguments @("/S") -TimeoutSeconds $InstallTimeoutSeconds -Name "install" | Out-Null
    Assert-Exists -Paths @(
        $InstallDirectory,
        (Join-Path $InstallDirectory "gui-for-cli-webui-tauri.exe"),
        (Join-Path $InstallDirectory "uninstall.exe"),
        (Join-Path $InstallDirectory "node\node.exe"),
        (Join-Path $InstallDirectory "examples\EmbeddedBundle")
    )
    return $InstallDirectory
}

function Uninstall-App {
    $uninstaller = Join-Path $InstallDirectory "uninstall.exe"
    if (Test-Path -LiteralPath $uninstaller) {
        Invoke-External -FilePath $uninstaller -Arguments @("/S") -TimeoutSeconds $InstallTimeoutSeconds -Name "uninstall" | Out-Null
    }
    Remove-Tree -LiteralPath $InstallDirectory
    Assert-NotExists -Paths @($InstallDirectory)
    return $InstallDirectory
}

function Clear-InstallData {
    foreach ($path in @($InstallDirectory, "$env:LOCALAPPDATA\GUI for CLI WebUI", $WebViewDataDirectory, $WorkspaceDirectory)) {
        Remove-Tree -LiteralPath $path
    }
    Assert-NotExists -Paths @($InstallDirectory, "$env:LOCALAPPDATA\GUI for CLI WebUI", $WebViewDataDirectory, $WorkspaceDirectory)
    return "cleared"
}

function Start-InstalledApp {
    Remove-Item -LiteralPath $PortFile -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $TokenFile -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $SetupLog -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $SetupSummary -ErrorAction SilentlyContinue
    $appPath = Join-Path $InstallDirectory "gui-for-cli-webui-tauri.exe"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $appPath
    $psi.WorkingDirectory = $InstallDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    if ($null -ne $psi.Environment) {
        $psi.Environment["GFC_PORT_FILE"] = $PortFile
        $psi.Environment["GUI_FOR_CLI_AUTO_RUN_SETUP"] = "1"
        $psi.Environment["GUI_FOR_CLI_SETUP_EVENTS_LOG"] = $SetupLog
        if ($AutomatedAdmin) {
            $psi.Environment["GUI_FOR_CLI_WINDOWS_ADMIN_MODE"] = "scheduled-task"
            $psi.Environment["GUI_FOR_CLI_WINDOWS_ADMIN_TASK"] = [string]$script:AdminBroker.taskName
            $psi.Environment["GUI_FOR_CLI_WINDOWS_ADMIN_QUEUE"] = [string]$script:AdminBroker.queueDirectory
        }
    } else {
        $psi.EnvironmentVariables["GFC_PORT_FILE"] = $PortFile
        $psi.EnvironmentVariables["GUI_FOR_CLI_AUTO_RUN_SETUP"] = "1"
        $psi.EnvironmentVariables["GUI_FOR_CLI_SETUP_EVENTS_LOG"] = $SetupLog
        if ($AutomatedAdmin) {
            $psi.EnvironmentVariables["GUI_FOR_CLI_WINDOWS_ADMIN_MODE"] = "scheduled-task"
            $psi.EnvironmentVariables["GUI_FOR_CLI_WINDOWS_ADMIN_TASK"] = [string]$script:AdminBroker.taskName
            $psi.EnvironmentVariables["GUI_FOR_CLI_WINDOWS_ADMIN_QUEUE"] = [string]$script:AdminBroker.queueDirectory
        }
    }
    $process = [System.Diagnostics.Process]::Start($psi)

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "Installed app exited during startup with code $($process.ExitCode)."
        }
        if (Test-Path -LiteralPath $PortFile) {
            $raw = Get-Content -LiteralPath $PortFile -Raw -ErrorAction SilentlyContinue
            if ($raw) {
                $port = $raw.Trim()
                if ($port) {
                    # The app writes the token file before the port file.
                    $token = (Get-Content -LiteralPath $TokenFile -Raw -ErrorAction Stop).Trim()
                    if (-not $token) {
                        throw "Installed app wrote an empty API token file: $TokenFile"
                    }
                    return [ordered]@{ process = $process; port = [int]$port; token = $token }
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "Installed app did not write port file within $StartupTimeoutSeconds seconds."
}

function Invoke-UISetupValidation {
    param([int]$Port, [string]$Token)

    if ($SkipSetup) {
        return "skipped (ui)"
    }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        throw "Node.js was not found on PATH; -DriveUI requires repo-level node + playwright (`cd app && npm install`)."
    }
    $driverScript = (Resolve-Path (Join-Path $PSScriptRoot "ui-driver-lifecycle.mjs")).Path
    $playwrightDir = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "app\node_modules\playwright"
    if (-not (Test-Path -LiteralPath $playwrightDir)) {
        throw "Playwright is not installed under app/node_modules. Run: cd app && npm install"
    }

    $uiLog = Join-Path $LogDirectory "ui-driver.log"
    Remove-Item -LiteralPath $uiLog -ErrorAction SilentlyContinue
    Write-Host "  UI driver: launching Chromium at http://127.0.0.1:$Port (log: $uiLog)"

    $env:TEST_PORT = [string]$Port
    $env:TEST_TOKEN = $Token
    $env:UI_LOG = $uiLog
    $env:UI_PAGES = $UIPages
    $env:UI_SETUP_TIMEOUT_SECONDS = [string]$UISetupTimeoutSeconds
    try {
        & $nodeCommand.Source $driverScript 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) {
            throw "UI driver exited with code $LASTEXITCODE (log: $uiLog)"
        }
    }
    finally {
        Remove-Item Env:TEST_PORT -ErrorAction SilentlyContinue
        Remove-Item Env:TEST_TOKEN -ErrorAction SilentlyContinue
        Remove-Item Env:UI_LOG -ErrorAction SilentlyContinue
        Remove-Item Env:UI_PAGES -ErrorAction SilentlyContinue
        Remove-Item Env:UI_SETUP_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
    }

    $runtime = Join-Path $WorkspaceDirectory "runtime\wgsextract-cli"
    Assert-Exists -Paths @(
        $WorkspaceDirectory,
        $runtime,
        (Join-Path $runtime "app"),
        (Join-Path $runtime "bin"),
        (Join-Path $runtime "bin\wgsextract.cmd")
    )
    return "ui setup ok"
}

function Invoke-SetupValidation {
    param([int]$Port)

    if ($SkipSetup) {
        return "skipped"
    }

    # The installed app's client auto-runs setup on launch (GUI_FOR_CLI_AUTO_RUN_SETUP=1).
    # The server writes every event to $SetupLog via GUI_FOR_CLI_SETUP_EVENTS_LOG.
    # We act as a pure observer: tail the file, echo each event live, and wait for
    # the terminal "complete" event before returning.
    Write-Host "  setup observer: tailing $SetupLog (server-driven by app auto-run)"

    $deadline = (Get-Date).AddSeconds($SetupTimeoutSeconds)
    while (-not (Test-Path -LiteralPath $SetupLog) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path -LiteralPath $SetupLog)) {
        throw "Setup event log was never written: $SetupLog"
    }

    $stream = [System.IO.File]::Open($SetupLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.UTF8Encoding]::new($false))
    $complete = $null
    $pending = ""
    try {
        while ($null -eq $complete -and (Get-Date) -lt $deadline) {
            $chunk = $reader.ReadToEnd()
            if ($chunk.Length -gt 0) {
                $pending += $chunk
                $parts = $pending -split "`r?`n"
                $pending = $parts[-1]
                for ($i = 0; $i -lt $parts.Length - 1; $i++) {
                    $line = $parts[$i]
                    if ([string]::IsNullOrWhiteSpace($line)) { continue }
                    try { $event = $line | ConvertFrom-Json } catch { continue }
                    switch ($event.type) {
                        "step-start" { Write-Host "  [setup] ==> $($event.step.label)" }
                        "output" {
                            $text = [string]$event.text
                            foreach ($outLine in ($text -split "`r?`n")) {
                                if ($outLine.Length -gt 0) {
                                    Write-Host "  [$($event.id)|$($event.stream)] $outLine"
                                }
                            }
                        }
                        "step-complete" { Write-Host "  [setup] <== $($event.result.id) $($event.result.status) (exit=$($event.result.exitCode))" }
                        "complete" { $complete = $event.result; Write-Host "  [setup] === ALL DONE: status=$($complete.status)" }
                    }
                }
            } else {
                Start-Sleep -Milliseconds 250
            }
        }
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }

    if ($null -eq $complete) {
        throw "Setup did not complete within $SetupTimeoutSeconds seconds (no 'complete' event in $SetupLog)"
    }

    Write-Utf8File -LiteralPath $SetupSummary -Value (($complete | ConvertTo-Json -Depth 10) + "`n")
    $failed = @($complete.results | Where-Object { $_.status -ne 'ok' })
    if ($complete.status -ne 'ok' -or $failed.Count -gt 0) {
        $details = ($failed | ForEach-Object { "$($_.id):$($_.status):$($_.exitCode)" }) -join ', '
        throw "setup status=$($complete.status); failed=$details (log: $SetupLog)"
    }

    $runtime = Join-Path $WorkspaceDirectory "runtime\wgsextract-cli"
    Assert-Exists -Paths @(
        $WorkspaceDirectory,
        $runtime,
        (Join-Path $runtime "app"),
        (Join-Path $runtime "bin"),
        (Join-Path $runtime "bin\wgsextract.cmd")
    )
    return "setup ok"
}

function Invoke-UninstallValidation {
    param([int]$Port, [string]$Token)

    $nodePath = Join-Path $InstallDirectory "node\node.exe"
    $helper = Join-Path $LogDirectory "stream-uninstall.mjs"
    $helperSource = @'
import { writeFileSync } from 'node:fs';

const port = process.env.TEST_PORT;
const token = process.env.TEST_TOKEN;
const logPath = process.env.UNINSTALL_LOG;
const summaryPath = process.env.UNINSTALL_SUMMARY;
const timeoutMs = Number(process.env.UNINSTALL_TIMEOUT_SECONDS ?? '1800') * 1000;
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(new Error('uninstall timeout')), timeoutMs);

try {
  const response = await fetch(`http://127.0.0.1:${port}/api/uninstall/stream`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-gfc-token': token },
    body: JSON.stringify({ locale: '' }),
    signal: controller.signal,
  });
  if (!response.ok) {
    throw new Error(`uninstall HTTP ${response.status}: ${await response.text()}`);
  }
  const decoder = new TextDecoder();
  let buffer = '';
  let log = '';
  let complete;
  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.trim()) continue;
      log += `${line}\n`;
      const event = JSON.parse(line);
      if (event.type === 'complete') complete = event.result;
    }
  }
  if (buffer.trim()) {
    log += `${buffer}\n`;
    const event = JSON.parse(buffer);
    if (event.type === 'complete') complete = event.result;
  }
  writeFileSync(logPath, log, 'utf8');
  if (!complete) {
    throw new Error('uninstall stream ended without a complete event');
  }
  writeFileSync(summaryPath, `${JSON.stringify(complete, null, 2)}\n`, 'utf8');
  const failed = (complete.results ?? []).filter((result) => result.status !== 'ok');
  if (complete.status !== 'ok' || failed.length > 0) {
    console.error(`uninstall status=${complete.status}; failed=${failed.map((result) => `${result.id}:${result.status}:${result.exitCode}`).join(', ')}`);
    process.exit(1);
  }
} catch (error) {
  console.error(error?.message ?? String(error));
  process.exit(1);
} finally {
  clearTimeout(timeout);
}
'@
    Write-Utf8File -LiteralPath $helper -Value $helperSource
    Invoke-External `
        -FilePath $nodePath `
        -Arguments @($helper) `
        -TimeoutSeconds ($SetupTimeoutSeconds + 30) `
        -Environment @{
            TEST_PORT = $Port
            TEST_TOKEN = $Token
            UNINSTALL_LOG = $UninstallLog
            UNINSTALL_SUMMARY = $UninstallSummary
            UNINSTALL_TIMEOUT_SECONDS = $SetupTimeoutSeconds
        } `
        -Name "uninstall-stream" | Out-Null

    Assert-NotExists -Paths @(
        (Join-Path $WorkspaceDirectory "runtime\wgsextract-cli")
    )
    return "bundle uninstall ok"
}

function Stop-InstalledApp {
    param([System.Diagnostics.Process]$Process)

    if (-not $Process.HasExited) {
        [void]$Process.CloseMainWindow()
        if (-not $Process.WaitForExit($ShutdownTimeoutSeconds * 1000)) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            throw "Installed app did not exit within $ShutdownTimeoutSeconds seconds after window close."
        }
    }
    Start-Sleep -Seconds 2
    $remaining = @(Get-TargetProcesses)
    if ($remaining.Count -gt 0) {
        throw "Processes remained after close: $($remaining.ProcessId -join ', ')"
    }
    return "app exited and processes cleaned up"
}

if ($RemoveAdminBroker) {
    Remove-WindowsAdminBroker
    Write-Host "Admin broker task '$AdminTaskName' removed."
    return
}

if ($PrepareAdminBroker) {
    $script:AdminBroker = Ensure-WindowsAdminBroker
    Write-Host "Admin broker task '$($script:AdminBroker.taskName)' is ready with queue '$($script:AdminBroker.queueDirectory)'."
    return
}

$appState = $null
try {
    if ($AutomatedAdmin) {
        Invoke-Stage -Name "prepare-admin-broker" -ScriptBlock {
            $script:AdminBroker = Ensure-WindowsAdminBroker
            "task=$($script:AdminBroker.taskName) queue=$($script:AdminBroker.queueDirectory)"
        }
    }
    Invoke-Stage -Name "stop-existing-processes" -ScriptBlock { Stop-TargetProcesses }
    Invoke-Stage -Name "uninstall-existing" -ScriptBlock { Uninstall-App }
    Invoke-Stage -Name "clear-install-data" -ScriptBlock { Clear-InstallData }
    Invoke-Stage -Name "install" -ScriptBlock { Install-App }
    Invoke-Stage -Name "launch" -ScriptBlock {
        $script:appState = Start-InstalledApp
        "pid=$($script:appState.process.Id) port=$($script:appState.port)"
    }
    if ($DriveUI) {
        Invoke-Stage -Name "ui-walkthrough" -ScriptBlock { Invoke-UISetupValidation -Port $script:appState.port -Token $script:appState.token }
    }
    else {
        Invoke-Stage -Name "setup" -ScriptBlock { Invoke-SetupValidation -Port $script:appState.port }
    }
    Invoke-Stage -Name "bundle-uninstall" -ScriptBlock { Invoke-UninstallValidation -Port $script:appState.port -Token $script:appState.token }
    Invoke-Stage -Name "close-window" -ScriptBlock { Stop-InstalledApp -Process $script:appState.process }
    if (-not $KeepInstalled) {
        Invoke-Stage -Name "uninstall" -ScriptBlock { Uninstall-App }
        Invoke-Stage -Name "post-uninstall-process-check" -ScriptBlock { Stop-TargetProcesses }
        Invoke-Stage -Name "clear-bundle-workspace" -ScriptBlock {
            Remove-Tree -LiteralPath $WorkspaceDirectory
            Assert-NotExists -Paths @($WorkspaceDirectory)
            "workspace removed"
        }
    }
    Write-Summary
}
catch {
    if ($appState -and $appState.process -and -not $appState.process.HasExited) {
        Stop-Process -Id $appState.process.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-TargetProcesses | Out-Null
    throw
}
