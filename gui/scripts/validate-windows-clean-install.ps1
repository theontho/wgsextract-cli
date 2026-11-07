# End-to-end clean-install validator for the Windows Tauri WGSExtract bundle.
#
# Per cycle:
#   1. Wipe machine-scope prerequisites we (or upstream install_windows.bat) install:
#      MSYS2 root, ~\.pixi, stale User PATH entries, bundle workspace dir.
#   2. Assert the machine is in the expected pristine state.
#   3. Run validate-windows-tauri-lifecycle.ps1 -AutomatedAdmin, which installs the
#      NSIS app, runs the bundle setup-stream (calls install_windows.bat under the
#      hood), runs the bundle uninstall-stream, and uninstalls the app.
#   4. Parse setup/uninstall summary JSON: every stage must report "ok".
#   5. Assert the machine is back to pristine state.
#
# Asks for UAC once at script start (admin is required to kill SYSTEM-owned
# MSYS2 helper processes — gpg-agent, dirmngr — left over by the broker's
# elevated bash session). Once elevated, all cycles run unattended.

[CmdletBinding()]
param(
    [int]$Cycles = 1,
    [string]$InstallerPath = "",
    [string]$LogDirectory = "tmp\windows-clean-install",
    [int]$LifecycleTimeoutSeconds = 1800,
    [string]$Msys2Root = "$env:LOCALAPPDATA\msys64",
    [string]$PixiRoot = "$env:USERPROFILE\.pixi",
    [string]$PixiBaseDir = "$env:LOCALAPPDATA\WGSExtractPixi",
    [string]$WorkspaceDirectory = "$env:LOCALAPPDATA\dev.guiforcli.web.embed.wgsextract\BundleWorkspaces\wgs-extract",
    [string]$InstallDirectory = "$env:LOCALAPPDATA\WGSExtract",
    [string]$AdminTaskName = "GUIForCLIWindowsAdminBroker",
    [string]$AdminQueueDirectory = "tmp\windows-admin-broker",
    [switch]$SkipPreCleanup,
    [switch]$KeepInstalled,
    [switch]$PrepareAdminBroker,
    [switch]$Elevated
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

function Resolve-Pwsh {
    $cmd = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "pwsh.exe (PowerShell 7+) is required. Install from https://github.com/PowerShell/PowerShell/releases" }
    return $cmd.Source
}
$script:PwshPath = Resolve-Pwsh

function Test-RunningElevated {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-AdminBrokerInstalled {
    $task = Get-ScheduledTask -TaskName $AdminTaskName -ErrorAction SilentlyContinue
    return [bool]$task
}

function Format-StartProcessArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($char in $Value.ToCharArray()) {
        if ($char -eq '\') {
            $backslashes += 1
            continue
        }
        if ($char -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($char)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Join-StartProcessArgumentList {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    return (($Arguments | ForEach-Object { Format-StartProcessArgument -Value $_ }) -join ' ')
}

if ($PrepareAdminBroker) {
    if (-not (Test-RunningElevated)) {
        Write-Host "[info] -PrepareAdminBroker requires elevation; relaunching via UAC (one-time)."
        $proc = Start-Process -FilePath $script:PwshPath -ArgumentList (Join-StartProcessArgumentList -Arguments @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath,
            '-PrepareAdminBroker', '-Elevated',
            '-AdminTaskName', $AdminTaskName, '-AdminQueueDirectory', $AdminQueueDirectory
        )) -Verb RunAs -Wait -PassThru
        exit $proc.ExitCode
    }
    $lifecycleScript = (Resolve-Path (Join-Path $PSScriptRoot "validate-windows-tauri-lifecycle.ps1")).Path
    & $script:PwshPath -NoProfile -ExecutionPolicy Bypass -File $lifecycleScript `
        -PrepareAdminBroker -AdminTaskName $AdminTaskName -AdminQueueDirectory $AdminQueueDirectory
    exit $LASTEXITCODE
}

# If we aren't elevated and the broker task is installed, route the cleanup
# work through the broker — no UAC. If the broker isn't installed, fall back
# to a one-time UAC self-elevation. (Run with -PrepareAdminBroker once to
# install the broker and remove all UAC prompts thereafter.)
if (-not (Test-RunningElevated)) {
    if (Test-AdminBrokerInstalled) {
        Write-Host "[info] Not elevated; admin broker scheduled task '$AdminTaskName' is installed. Cycles will use it for admin work — no UAC."
        $script:UseAdminBroker = $true
    } else {
        Write-Host "[info] Not elevated and admin broker not installed. Relaunching once via UAC."
        Write-Host "[hint] Install the broker once to skip all future UAC prompts: pwsh -File $PSCommandPath -PrepareAdminBroker"
        $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File', $PSCommandPath, '-Elevated')
        foreach ($key in $PSBoundParameters.Keys) {
            if ($key -eq 'Elevated') { continue }
            $val = $PSBoundParameters[$key]
            if ($val -is [System.Management.Automation.SwitchParameter]) {
                if ($val.IsPresent) { $argList += "-$key" }
            } else {
                $argList += "-$key"
                $argList += [string]$val
            }
        }
        $proc = Start-Process -FilePath $script:PwshPath -ArgumentList (Join-StartProcessArgumentList -Arguments $argList) -Verb RunAs -Wait -PassThru
        exit $proc.ExitCode
    }
} else {
    $script:UseAdminBroker = $false
}

$script:LogDirectory = (New-Item -ItemType Directory -Force -Path $LogDirectory).FullName
$script:StartedAt = Get-Date
$script:CycleResults = [System.Collections.Generic.List[object]]::new()
$script:UserLocalAppData = $env:LOCALAPPDATA

function Write-Stage {
    param([string]$Message, [string]$Status = "info")

    $elapsed = [math]::Round(((Get-Date) - $script:StartedAt).TotalSeconds, 1)
    Write-Host "[$elapsed s] [$Status] $Message"
}

function Remove-TreeWithRetry {
    param([Parameter(Mandatory = $true)][string]$Path)

    for ($attempt = 1; $attempt -le 8; $attempt += 1) {
        if (-not (Test-Path -LiteralPath $Path)) { return }
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 8) { throw }
            # On retry, re-kill any MSYS2/Pixi processes that may have spawned or
            # be still holding file handles (e.g. dirmngr, gpg-agent), then wait.
            Stop-MsysProcesses
            Start-Sleep -Milliseconds (500 * $attempt)
        }
    }
}

function Stop-MsysProcesses {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and (
            $_.ExecutablePath -like "$Msys2Root\*" -or
            $_.ExecutablePath -like "$PixiRoot\*"
        )
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Msys2Purge {
    if (-not (Test-Path -LiteralPath $Msys2Root -PathType Container)) {
        Write-Stage "MSYS2 already absent at $Msys2Root"
        return
    }

    $maintenanceTool = Join-Path $Msys2Root "uninstall.exe"
    if (Test-Path -LiteralPath $maintenanceTool -PathType Leaf) {
        Write-Stage "Running MSYS2 maintenance tool purge at $Msys2Root"
        & $maintenanceTool purge --confirm-command --accept-messages 2>&1 | Out-Host
    }

    if (Test-Path -LiteralPath $Msys2Root) {
        Write-Stage "Removing residual MSYS2 directory $Msys2Root"
        Remove-TreeWithRetry -Path $Msys2Root
    }

    $registryKey = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MSYS2"
    if (Test-Path -LiteralPath $registryKey) {
        Remove-Item -LiteralPath $registryKey -Recurse -Force -ErrorAction SilentlyContinue
        Write-Stage "Cleaned HKCU uninstall registry entry for MSYS2"
    }
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\MSYS2"
    if (Test-Path -LiteralPath $startMenu -PathType Container) {
        Remove-TreeWithRetry -Path $startMenu
        Write-Stage "Cleaned MSYS2 Start Menu folder"
    }
}

function Remove-PixiInstall {
    if (Test-Path -LiteralPath $PixiRoot -PathType Container) {
        Write-Stage "Removing Pixi install at $PixiRoot"
        Remove-TreeWithRetry -Path $PixiRoot
    } else {
        Write-Stage "Pixi already absent at $PixiRoot"
    }

    $pixiBin = Join-Path $PixiRoot "bin"
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not $userPath) { return }
    $entries = $userPath -split ";"
    $kept = @($entries | Where-Object {
        $trimmed = $_.TrimEnd("\")
        $trimmed -ne $pixiBin.TrimEnd("\")
    })
    if ($kept.Count -ne $entries.Count) {
        [Environment]::SetEnvironmentVariable("PATH", ($kept -join ";"), "User")
        Write-Stage "Stripped Pixi bin from User PATH"
    }
}

function Invoke-AdminBroker {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptBody,
        [string]$Label = "admin-task",
        [int]$TimeoutSeconds = 600
    )

    $queueRoot = (New-Item -ItemType Directory -Force -Path $AdminQueueDirectory).FullName
    $stamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
    $launcherPath = Join-Path $queueRoot ("$Label.$stamp.launcher.ps1")
    $exitPath = Join-Path $queueRoot ("$Label.$stamp.exit")
    $stderrPath = Join-Path $queueRoot ("$Label.$stamp.stderr.log")
    $pendingPath = Join-Path $queueRoot ("$Label.$stamp.pending.json")
    [System.IO.File]::WriteAllText($launcherPath, $ScriptBody, [System.Text.UTF8Encoding]::new($false))
    $request = [ordered]@{
        launcherPath = $launcherPath
        exitCodePath = $exitPath
        stderrPath   = $stderrPath
    }
    [System.IO.File]::WriteAllText($pendingPath, ($request | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))

    Write-Stage "Submitting '$Label' to admin broker '$AdminTaskName'"
    & schtasks.exe /Run /TN $AdminTaskName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /Run failed for broker '$AdminTaskName' (exit $LASTEXITCODE). Run -PrepareAdminBroker first."
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $exitPath) {
            $rawCode = (Get-Content -LiteralPath $exitPath -Raw -ErrorAction SilentlyContinue)
            if ($rawCode) {
                $code = [int]($rawCode.Trim())
                if (Test-Path -LiteralPath $stderrPath) {
                    $err = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
                    if ($err) { Write-Host "[broker:$Label stderr] $err" }
                }
                Remove-Item -LiteralPath $pendingPath, $launcherPath, $exitPath, $stderrPath -ErrorAction SilentlyContinue
                if ($code -ne 0) { throw "Admin broker task '$Label' exited $code" }
                return
            }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Admin broker task '$Label' did not complete within $TimeoutSeconds seconds"
}

function Format-PowerShellSingleQuotedString {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return "'$($Value.Replace("'", "''"))'"
}

function Get-CleanupAdminScriptBody {
    # Render a self-contained script body that re-implements the admin-required
    # pieces of Pre-Cleanup. Runs as SYSTEM via the broker — no UAC.
    $msys2RootLiteral = Format-PowerShellSingleQuotedString -Value $Msys2Root
    $msys2RootGlobLiteral = Format-PowerShellSingleQuotedString -Value "$Msys2Root\*"
    $pixiRootGlobLiteral = Format-PowerShellSingleQuotedString -Value "$PixiRoot\*"
    $workspaceDirectoryLiteral = Format-PowerShellSingleQuotedString -Value $WorkspaceDirectory
    $pixiBaseDirLiteral = Format-PowerShellSingleQuotedString -Value $PixiBaseDir
    $installDirectoryLiteral = Format-PowerShellSingleQuotedString -Value $InstallDirectory
    $webViewDataDirectoryLiteral = Format-PowerShellSingleQuotedString -Value (Join-Path $script:UserLocalAppData "GUI for CLI WebUI")
    @"
`$ErrorActionPreference = 'Stop'
function Stop-MsysProcs {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        `$_.ExecutablePath -and (
            `$_.ExecutablePath -like $msys2RootGlobLiteral -or
            `$_.ExecutablePath -like $pixiRootGlobLiteral
        )
    } | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Remove-TreeRetry {
    param([string]`$Path)
    for (`$i = 1; `$i -le 8; `$i++) {
        if (-not (Test-Path -LiteralPath `$Path)) { return }
        try { Remove-Item -LiteralPath `$Path -Recurse -Force -ErrorAction Stop; return }
        catch { if (`$i -eq 8) { throw }; Stop-MsysProcs; Start-Sleep -Milliseconds (500 * `$i) }
    }
}
Stop-MsysProcs
if (Test-Path -LiteralPath $msys2RootLiteral) {
    `$tool = Join-Path $msys2RootLiteral 'uninstall.exe'
    if (Test-Path -LiteralPath `$tool -PathType Leaf) {
        & `$tool purge --confirm-command --accept-messages 2>&1 | Out-Null
    }
    Remove-TreeRetry -Path $msys2RootLiteral
}
foreach (`$p in @($workspaceDirectoryLiteral, $pixiBaseDirLiteral, $webViewDataDirectoryLiteral, $installDirectoryLiteral)) {
    if (Test-Path -LiteralPath `$p) { Remove-TreeRetry -Path `$p }
}
exit 0
"@
}

function Pre-Cleanup {
    if ($script:UseAdminBroker) {
        # Send all admin-needing cleanup to the broker; do the user-scope bits here.
        Invoke-AdminBroker -ScriptBody (Get-CleanupAdminScriptBody) -Label "pre-cleanup" -TimeoutSeconds 600
        Remove-PixiInstall
    } else {
        Stop-MsysProcesses
        Invoke-Msys2Purge
        Remove-PixiInstall
        foreach ($path in @($WorkspaceDirectory, $PixiBaseDir, "$env:LOCALAPPDATA\GUI for CLI WebUI")) {
            if (Test-Path -LiteralPath $path) {
                Write-Stage "Removing $path"
                Remove-TreeWithRetry -Path $path
            }
        }
    }
}

function Assert-CleanState {
    param([string]$Phase)

    $issues = @()
    if (Test-Path -LiteralPath $Msys2Root) { $issues += "MSYS2 root still present: $Msys2Root" }
    if (Test-Path -LiteralPath $PixiRoot) { $issues += "Pixi root still present: $PixiRoot" }
    if (Test-Path -LiteralPath $PixiBaseDir) { $issues += "Pixi base dir still present: $PixiBaseDir" }
    if (Test-Path -LiteralPath (Join-Path $WorkspaceDirectory "runtime\wgsextract-cli\app")) {
        $issues += "Bundle runtime still present: $WorkspaceDirectory\runtime\wgsextract-cli\app"
    }
    $bioTools = @("samtools", "bcftools", "tabix", "bgzip", "bwa", "htsfile") | ForEach-Object {
        $c = Get-Command $_ -ErrorAction SilentlyContinue
        if ($c -and $c.Source -like "$Msys2Root\*") { "$_ -> $($c.Source)" }
    }
    if ($bioTools) { $issues += "Bio tools still resolvable from MSYS2 root: $($bioTools -join '; ')" }

    if ($issues.Count -gt 0) {
        throw "$Phase clean-state check failed:`n  $($issues -join "`n  ")"
    }
    Write-Stage "$Phase clean-state OK" "ok"
}

function Invoke-Lifecycle {
    param([int]$CycleIndex)

    $lifecycleScript = (Resolve-Path (Join-Path $PSScriptRoot "validate-windows-tauri-lifecycle.ps1")).Path
    $cycleLogDir = Join-Path $script:LogDirectory "cycle-$CycleIndex"
    New-Item -ItemType Directory -Force -Path $cycleLogDir | Out-Null

    $lifecycleArgs = @(
        '-AutomatedAdmin',
        '-LogDirectory', $cycleLogDir,
        '-InstallDirectory', $InstallDirectory,
        '-WorkspaceDirectory', $WorkspaceDirectory,
        '-SetupTimeoutSeconds', $LifecycleTimeoutSeconds
    )
    if ($InstallerPath) { $lifecycleArgs += @('-InstallerPath', $InstallerPath) }
    if ($KeepInstalled) { $lifecycleArgs += '-KeepInstalled' }

    Write-Stage "Invoking lifecycle script for cycle $CycleIndex"
    & $script:PwshPath -NoProfile -ExecutionPolicy Bypass -File $lifecycleScript @lifecycleArgs
    if ($LASTEXITCODE -ne 0) {
        throw "validate-windows-tauri-lifecycle.ps1 exited $LASTEXITCODE for cycle $CycleIndex (logs at $cycleLogDir)"
    }

    Assert-LifecycleSummary -CycleLogDir $cycleLogDir
}

function Assert-LifecycleSummary {
    param([string]$CycleLogDir)

    foreach ($summary in @("installed-setup-summary.json", "installed-uninstall-summary.json")) {
        $path = Join-Path $CycleLogDir $summary
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Lifecycle did not produce $summary"
        }
        $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ($data.status -ne "ok") {
            throw "$summary reports status=$($data.status)"
        }
        $failed = @($data.results | Where-Object { $_.status -ne "ok" })
        if ($failed.Count -gt 0) {
            $detail = ($failed | ForEach-Object { "$($_.id):$($_.status):exitCode=$($_.exitCode)" }) -join ", "
            throw "$summary contains failed steps: $detail"
        }
    }
    Write-Stage "Lifecycle setup/uninstall summaries all green" "ok"
}

function Write-FinalSummary {
    $summaryPath = Join-Path $script:LogDirectory "summary.json"
    $payload = [ordered]@{
        startedAt = $script:StartedAt.ToString("o")
        completedAt = (Get-Date).ToString("o")
        cycles = $script:CycleResults
        installer = $InstallerPath
        msys2Root = $Msys2Root
        pixiRoot = $PixiRoot
        workspaceDirectory = $WorkspaceDirectory
        installDirectory = $InstallDirectory
    }
    $json = $payload | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($summaryPath, "$json`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Stage "Wrote summary $summaryPath" "ok"
}

$exitCode = 0
try {
    Write-Stage "Running $Cycles cycle(s) elevated. UAC will not prompt again."
    for ($cycle = 1; $cycle -le $Cycles; $cycle += 1) {
        $cycleStart = Get-Date
        Write-Stage "===== Cycle $cycle/$Cycles =====" "info"
        try {
            if (-not $SkipPreCleanup) {
                Write-Stage "Pre-cleanup: removing machine-scope prerequisites"
                Pre-Cleanup
            }
            Assert-CleanState -Phase "pre-install"
            Invoke-Lifecycle -CycleIndex $cycle
            Assert-CleanState -Phase "post-uninstall"
            $duration = [math]::Round(((Get-Date) - $cycleStart).TotalSeconds, 1)
            $script:CycleResults.Add([ordered]@{ cycle = $cycle; status = "ok"; seconds = $duration })
            Write-Stage "Cycle $cycle complete ($duration s)" "ok"
        } catch {
            $duration = [math]::Round(((Get-Date) - $cycleStart).TotalSeconds, 1)
            $script:CycleResults.Add([ordered]@{ cycle = $cycle; status = "failed"; seconds = $duration; error = $_.Exception.Message })
            Write-Stage "Cycle $cycle FAILED: $($_.Exception.Message)" "failed"
            $exitCode = 1
            break
        }
    }
} finally {
    Write-FinalSummary
}
exit $exitCode
