param(
    [string]$Repo = "WGSExtract/wgsextract-cli",
    [string]$LatestTag = "",
    [string]$OldVersion = "",
    [string]$UpdaterPublicKey = "",
    [string]$WorkDirectory = "tmp\tauri-update-e2e",
    [string]$OldPackageDirectory = "out\tauri-update-e2e\old",
    [int]$InstallTimeoutSeconds = 180,
    [int]$StartupTimeoutSeconds = 90,
    [int]$UpdateTimeoutSeconds = 600,
    [int]$ShutdownTimeoutSeconds = 30,
    [int]$VisibleLaunchSeconds = 12,
    [int]$VisiblePromptSeconds = 12,
    [int]$VisibleInstallSeconds = 8,
    [int]$VisibleRelaunchSeconds = 12,
    [switch]$RecordVideo,
    [switch]$KeepInstalled,
    [switch]$SkipOldBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

$script:StartedAt = Get-Date
$script:StageResults = [System.Collections.Generic.List[object]]::new()
$script:Summary = [ordered]@{}
$script:AppStdoutTask = $null
$script:AppStderrTask = $null
$script:AppStdoutPath = ""
$script:AppStderrPath = ""
$script:FfmpegStderrTask = $null
$script:FfmpegStderrPath = ""
$script:UpdaterPublicKeyResolved = ""
$script:quickUninstallPath = ""
$script:appName = "WGSExtract"
$script:appIdentifier = "dev.guiforcli.web.embed.wgsextract"
$script:appState = $null
$script:videoProcess = $null
$WorkDirectory = (New-Item -ItemType Directory -Force -Path $WorkDirectory).FullName
$OldPackageDirectory = [System.IO.Path]::GetFullPath($OldPackageDirectory)
$LatestAssetsDirectory = Join-Path $WorkDirectory "latest-release"
$PortFile = Join-Path $WorkDirectory "tauri-update-port.txt"
$UpdateStatusFile = Join-Path $WorkDirectory "tauri-update-status.log"
$SummaryPath = Join-Path $WorkDirectory "summary.json"
$VideoPath = Join-Path $WorkDirectory "windows-tauri-update-e2e.mp4"

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

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $utf8NoBom)
}

function Resolve-PowerShellHost {
    $pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwsh) {
        return $pwsh.Source
    }

    $powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if ($powershell) {
        return $powershell.Source
    }

    throw "Neither pwsh nor powershell.exe could be found."
}

$script:PowerShellHost = Resolve-PowerShellHost

function Quote-ProcessArgument {
    param([AllowEmptyString()][string]$Argument)

    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') {
        return $Argument
    }

    $quoted = New-Object System.Text.StringBuilder
    [void]$quoted.Append('"')
    $backslashes = 0
    foreach ($char in $Argument.ToCharArray()) {
        if ($char -eq '\') {
            $backslashes += 1
            continue
        }
        if ($char -eq '"') {
            [void]$quoted.Append(('\' * (($backslashes * 2) + 1)))
            [void]$quoted.Append('"')
        }
        else {
            if ($backslashes -gt 0) {
                [void]$quoted.Append(('\' * $backslashes))
            }
            [void]$quoted.Append($char)
        }
        $backslashes = 0
    }
    if ($backslashes -gt 0) {
        [void]$quoted.Append(('\' * ($backslashes * 2)))
    }
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

function Join-ProcessArguments {
    param([string[]]$Arguments = @())

    return (($Arguments | ForEach-Object { Quote-ProcessArgument -Argument $_ }) -join " ")
}

function Write-Summary {
    $summary = [ordered]@{
        startedAt = $script:StartedAt.ToString("o")
        completedAt = (Get-Date).ToString("o")
        stages = $script:StageResults
        workDirectory = $WorkDirectory
        oldPackageDirectory = $OldPackageDirectory
        portFile = $PortFile
        updateStatusFile = $UpdateStatusFile
        video = if (Test-Path -LiteralPath $VideoPath) { $VideoPath } else { $null }
    }
    foreach ($key in $script:Summary.Keys) {
        $summary[$key] = $script:Summary[$key]
    }
    Write-Utf8File -LiteralPath $SummaryPath -Value (($summary | ConvertTo-Json -Depth 8) + "`n")
    Write-Host "Summary: $SummaryPath"
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

    $stdoutPath = Join-Path $WorkDirectory "$($Name -replace '[^A-Za-z0-9_.-]', '_').stdout.log"
    $stderrPath = Join-Path $WorkDirectory "$($Name -replace '[^A-Za-z0-9_.-]', '_').stderr.log"
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -ErrorAction SilentlyContinue

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.Arguments = Join-ProcessArguments -Arguments $Arguments
    foreach ($key in $Environment.Keys) {
        $psi.Environment[$key] = [string]$Environment[$key]
    }

    $process = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "$Name timed out after $TimeoutSeconds seconds."
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    Write-Utf8File -LiteralPath $stdoutPath -Value $stdout
    Write-Utf8File -LiteralPath $stderrPath -Value $stderr
    if ($process.ExitCode -ne 0) {
        throw "$Name exited $($process.ExitCode). stdout=$stdoutPath stderr=$stderrPath"
    }
    return [ordered]@{ stdout = $stdoutPath; stderr = $stderrPath; exitCode = $process.ExitCode }
}

function Invoke-JsonCommand {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $result = Invoke-External -FilePath "gh" -Arguments $Arguments -TimeoutSeconds 120 -Name "gh"
    return Get-Content -LiteralPath $result.stdout -Raw | ConvertFrom-Json
}

function Resolve-LatestRelease {
    if ($LatestTag) {
        return Invoke-JsonCommand -Arguments @("release", "view", $LatestTag, "--repo", $Repo, "--json", "tagName,assets")
    }
    return Invoke-JsonCommand -Arguments @("release", "view", "--repo", $Repo, "--json", "tagName,assets")
}

function New-OlderPatchVersion {
    param([Parameter(Mandatory = $true)][string]$Version)

    $match = [regex]::Match($Version, '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)(?<suffix>.*)$')
    if (-not $match.Success) {
        throw "Latest release version is not a supported SemVer patch version: $Version"
    }
    $major = [int]$match.Groups["major"].Value
    $minor = [int]$match.Groups["minor"].Value
    $patch = [int]$match.Groups["patch"].Value
    if ($patch -gt 0) {
        return "$major.$minor.$($patch - 1)"
    }
    if ($minor -gt 0) {
        return "$major.$($minor - 1).999"
    }
    throw "Cannot synthesize an older patch version for $Version"
}

function Assert-OlderVersion {
    param([string]$Old, [string]$Latest)

    $oldVersion = [version]$Old
    $latestVersion = [version]$Latest
    if ($oldVersion -ge $latestVersion) {
        throw "Fake old version $Old must be older than latest release $Latest."
    }
}

function Get-AssetNameFromUrl {
    param([Parameter(Mandatory = $true)][string]$Url)

    return [System.Uri]::UnescapeDataString(([Uri]$Url).Segments[-1])
}

function Get-AssignmentValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $pattern = "^\`$$([regex]::Escape($Name))\s*=\s*'(?<value>.*)'\s*$"
    foreach ($line in Get-Content -LiteralPath $Path) {
        $match = [regex]::Match($line, $pattern)
        if ($match.Success) {
            return $match.Groups["value"].Value.Replace("''", "'")
        }
    }
    return ""
}

function Resolve-UpdaterPublicKey {
    if ($UpdaterPublicKey) {
        return $UpdaterPublicKey.Trim()
    }
    if ($env:TAURI_UPDATER_PUBKEY) {
        return $env:TAURI_UPDATER_PUBKEY.Trim()
    }

    $result = Invoke-External -FilePath "gh" -Arguments @("variable", "get", "TAURI_UPDATER_PUBKEY", "--repo", $Repo) -TimeoutSeconds 60 -Name "gh-variable-tauri-updater-pubkey"
    $key = (Get-Content -LiteralPath $result.stdout -Raw).Trim()
    if (-not $key) {
        throw "TAURI_UPDATER_PUBKEY is empty. Pass -UpdaterPublicKey or configure the repository variable."
    }
    return $key
}

function Get-TargetProcesses {
    param([string]$AppName = "", [string]$AppIdentifier = "")

    $needles = @($AppName, $AppIdentifier, "gui-for-cli-webui-tauri", "\wgs-extract\", "/wgs-extract/") |
        Where-Object { $_ } |
        Select-Object -Unique
    Get-CimInstance Win32_Process | Where-Object {
        $processCommandLine = $_.CommandLine
        $processName = $_.Name
        $_.ProcessId -ne $PID `
            -and $processCommandLine `
            -and $processName -notmatch '(?i)(setup|installer|uninstall)' `
            -and $processCommandLine -notmatch '(?i)(setup|installer|updater|uninstall)\.exe' `
            -and ($needles | Where-Object { $processCommandLine -like "*$_*" })
    } | Select-Object ProcessId, ParentProcessId, Name, CommandLine
}

function Stop-TargetProcesses {
    param([string]$AppName = "", [string]$AppIdentifier = "")

    $processes = @(Get-TargetProcesses -AppName $AppName -AppIdentifier $AppIdentifier)
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $remaining = @(Get-TargetProcesses -AppName $AppName -AppIdentifier $AppIdentifier)
    if ($remaining.Count -gt 0) {
        throw "Target processes still running: $($remaining.ProcessId -join ', ')"
    }
    return "stopped $($processes.Count) process(es)"
}

function Remove-Tree {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    for ($attempt = 1; $attempt -le 6; $attempt += 1) {
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
            if ($attempt -eq 6) {
                throw
            }
            Start-Sleep -Milliseconds (250 * $attempt)
        }
    }
}

function Invoke-QuickUninstall {
    param([string]$QuickUninstallPath)

    if (-not $QuickUninstallPath -or -not (Test-Path -LiteralPath $QuickUninstallPath -PathType Leaf)) {
        return "no quick uninstall"
    }
    Invoke-External -FilePath $script:PowerShellHost -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $QuickUninstallPath) -TimeoutSeconds $InstallTimeoutSeconds -Name "quick-uninstall" | Out-Null
    return "quick uninstall ok"
}

function Resolve-InstalledApp {
    param([string]$AppName)

    $candidates = @()
    if ($env:LOCALAPPDATA) {
        foreach ($name in @($AppName, "WGSExtract", "WGSExtract Web", "WGSExtract Windows WebUI") | Where-Object { $_ } | Select-Object -Unique) {
            $candidates += Join-Path $env:LOCALAPPDATA $name
        }
    }
    foreach ($directory in $candidates) {
        $exe = Join-Path $directory "gui-for-cli-webui-tauri.exe"
        if (Test-Path -LiteralPath $exe -PathType Leaf) {
            return [ordered]@{ installDirectory = $directory; executable = $exe }
        }
    }
    if ($env:LOCALAPPDATA) {
        $found = Get-ChildItem -LiteralPath $env:LOCALAPPDATA -Filter "gui-for-cli-webui-tauri.exe" -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($found) {
            return [ordered]@{ installDirectory = $found.DirectoryName; executable = $found.FullName }
        }
    }
    throw "Installed Tauri executable was not found under LOCALAPPDATA."
}

function Get-InstalledVersion {
    param([Parameter(Mandatory = $true)][string]$Executable)

    $versionInfo = (Get-Item -LiteralPath $Executable).VersionInfo
    foreach ($candidate in @($versionInfo.ProductVersion, $versionInfo.FileVersion)) {
        if ($candidate -and $candidate -match '\d+\.\d+\.\d+') {
            return $Matches[0]
        }
    }
    throw "Could not determine installed app version from $Executable."
}

function Wait-InstalledVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    $deadline = (Get-Date).AddSeconds($UpdateTimeoutSeconds)
    $lastVersion = "missing"
    do {
        if (Test-Path -LiteralPath $Executable -PathType Leaf) {
            try {
                $actual = Get-InstalledVersion -Executable $Executable
            }
            catch {
                $lastVersion = "error: $($_.Exception.Message)"
                Start-Sleep -Seconds 2
                continue
            }
            $lastVersion = $actual
            if ($actual -eq $ExpectedVersion) {
                return $actual
            }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Installed app did not reach version $ExpectedVersion within $UpdateTimeoutSeconds seconds. Last version: $lastVersion"
}

function Wait-InstalledRuntimeReady {
    param([Parameter(Mandatory = $true)][string]$InstallDirectory)

    $node = Join-Path $InstallDirectory "node\node.exe"
    $deadline = (Get-Date).AddSeconds($UpdateTimeoutSeconds)
    do {
        if (Test-Path -LiteralPath $node -PathType Leaf) {
            $nodeItem = Get-Item -LiteralPath $node
            if ($nodeItem.Length -gt 1MB) {
                try {
                    $versionOutput = & $node --version
                    if ($LASTEXITCODE -eq 0 -and $versionOutput -match '^v\d+\.\d+\.\d+') {
                        return "node=$($versionOutput.Trim()) bytes=$($nodeItem.Length)"
                    }
                }
                catch {
                    Start-Sleep -Seconds 1
                    continue
                }
            }
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    $lastSize = if (Test-Path -LiteralPath $node -PathType Leaf) { (Get-Item -LiteralPath $node).Length } else { "missing" }
    throw "Installed Node runtime was not usable within $UpdateTimeoutSeconds seconds: $node size=$lastSize"
}

function Start-InstalledApp {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$InstallDirectory,
        [bool]$AutoUpdate,
        [bool]$AutoAcceptUpdate = $true,
        [int]$AutoUpdateActionDelaySeconds = 0
    )

    Remove-Item -LiteralPath $PortFile -ErrorAction SilentlyContinue
    if ($AutoUpdate) {
        Remove-Item -LiteralPath $UpdateStatusFile -ErrorAction SilentlyContinue
    }

    $stdoutPath = Join-Path $WorkDirectory "installed-app.stdout.log"
    $stderrPath = Join-Path $WorkDirectory "installed-app.stderr.log"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Executable
    $psi.WorkingDirectory = $InstallDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $false
    $psi.Environment["GFC_PORT_FILE"] = $PortFile
    $psi.Environment["GFC_TAURI_UPDATE_STATUS_FILE"] = $UpdateStatusFile
    if ($RecordVideo) {
        $psi.Environment["GFC_TAURI_E2E_UPDATE_OVERLAY"] = "1"
        $psi.Environment["GFC_TAURI_E2E_STEP_DELAY_SECONDS"] = [string]$VisibleInstallSeconds
    }
    if ($AutoUpdate) {
        $psi.Environment["GFC_TAURI_AUTO_UPDATE"] = "1"
        if ($RecordVideo) {
            $psi.Environment["GFC_TAURI_AUTO_UPDATE_DELAY_SECONDS"] = [string]$VisibleLaunchSeconds
            $psi.Environment["GFC_TAURI_AUTO_UPDATE_ACTION_DELAY_SECONDS"] = [string]$AutoUpdateActionDelaySeconds
        }
        if ($AutoAcceptUpdate) {
            $psi.Environment["GFC_TAURI_AUTO_ACCEPT_UPDATE"] = "1"
        }
    }
    $process = [System.Diagnostics.Process]::Start($psi)
    $script:AppStdoutTask = $process.StandardOutput.ReadToEndAsync()
    $script:AppStderrTask = $process.StandardError.ReadToEndAsync()
    $script:AppStdoutPath = $stdoutPath
    $script:AppStderrPath = $stderrPath

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            Save-AppLogs
            throw "Installed app exited during startup with code $($process.ExitCode)."
        }
        if (Test-Path -LiteralPath $PortFile) {
            $port = (Get-Content -LiteralPath $PortFile -Raw).Trim()
            if ($port) {
                return [ordered]@{ process = $process; port = [int]$port }
            }
        }
        Start-Sleep -Milliseconds 250
    }

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Save-AppLogs
    throw "Installed app did not write port file within $StartupTimeoutSeconds seconds."
}

function Wait-UpdateStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Pattern,
        [string]$Description = $Pattern
    )

    $deadline = (Get-Date).AddSeconds($UpdateTimeoutSeconds)
    do {
        if (Test-Path -LiteralPath $UpdateStatusFile -PathType Leaf) {
            $statuses = @(Get-Content -LiteralPath $UpdateStatusFile)
            $failure = $statuses | Where-Object { $_ -match '^(check|prompt|download|install)-failed|^not-configured:' } | Select-Object -Last 1
            if ($failure) {
                throw "Update failed: $failure"
            }
            $match = $statuses | Where-Object { $_ -match $Pattern } | Select-Object -Last 1
            if ($match) {
                return ,$statuses
            }
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    $observed = if (Test-Path -LiteralPath $UpdateStatusFile) { (Get-Content -LiteralPath $UpdateStatusFile) -join ", " } else { "no status file" }
    throw "Timed out waiting for update status $Description within $UpdateTimeoutSeconds seconds. Observed: $observed"
}

function Save-AppLogs {
    if ($script:AppStdoutTask) {
        Write-Utf8File -LiteralPath $script:AppStdoutPath -Value $script:AppStdoutTask.GetAwaiter().GetResult()
    }
    if ($script:AppStderrTask) {
        Write-Utf8File -LiteralPath $script:AppStderrPath -Value $script:AppStderrTask.GetAwaiter().GetResult()
    }
}

function Wait-UpdateInstallStatus {
    return Wait-UpdateStatus -Pattern '^(installed:requesting-restart|installer-launched:exiting)$' -Description "completed install"
}

function Assert-WebUIUpdateFlow {
    param([string[]]$Statuses)

    $requiredPatterns = @(
        '^checking$',
        '^available:',
        '^accepted:auto$',
        '^downloading$',
        '^downloaded$',
        '^installing$',
        '^(installed:requesting-restart|installer-launched:exiting)$'
    )
    $cursor = 0
    foreach ($pattern in $requiredPatterns) {
        $matched = $false
        while ($cursor -lt $Statuses.Count) {
            if ($Statuses[$cursor] -match $pattern) {
                $cursor += 1
                $matched = $true
                break
            }
            $cursor += 1
        }
        if (-not $matched) {
            throw "Update status log did not prove the WebUI download/install flow. Missing ordered status '$pattern'. Observed: $($Statuses -join ', ')"
        }
    }
}

function Stop-InstalledApp {
    param([System.Diagnostics.Process]$Process)

    if ($Process -and -not $Process.HasExited) {
        [void]$Process.CloseMainWindow()
        if (-not $Process.WaitForExit($ShutdownTimeoutSeconds * 1000)) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Save-AppLogs
    return "stopped"
}

function Start-VideoRecording {
    if (-not $RecordVideo) {
        return $null
    }
    $ffmpeg = Get-Command ffmpeg -ErrorAction Stop
    Remove-Item -LiteralPath $VideoPath -ErrorAction SilentlyContinue
    $stderrPath = Join-Path $WorkDirectory "ffmpeg.stderr.log"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $ffmpeg.Source
    $psi.WorkingDirectory = $WorkDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.Arguments = Join-ProcessArguments -Arguments @("-y", "-f", "gdigrab", "-framerate", "10", "-i", "desktop", "-vcodec", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", $VideoPath)
    $process = [System.Diagnostics.Process]::Start($psi)
    $script:FfmpegStderrTask = $process.StandardError.ReadToEndAsync()
    $script:FfmpegStderrPath = $stderrPath
    Start-Sleep -Seconds 2
    return $process
}

function Stop-VideoRecording {
    param($Process)

    if (-not $Process) {
        return "not requested"
    }
    if (-not $Process.HasExited) {
        $Process.StandardInput.WriteLine("q")
        if (-not $Process.WaitForExit(10000)) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Utf8File -LiteralPath $script:FfmpegStderrPath -Value $script:FfmpegStderrTask.GetAwaiter().GetResult()
    if (-not (Test-Path -LiteralPath $VideoPath -PathType Leaf)) {
        throw "ffmpeg did not create video: $VideoPath"
    }
    return $VideoPath
}

function Build-OldInstaller {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][string]$PublicKey,
        [Parameter(Mandatory = $true)][string]$Endpoint
    )

    if ($SkipOldBuild) {
        $candidate = Get-ChildItem -LiteralPath $OldPackageDirectory -Filter "*-setup.exe" -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if (-not $candidate) {
            throw "-SkipOldBuild was set, but no old setup.exe was found in $OldPackageDirectory."
        }
        return $candidate.FullName
    }

    Remove-Tree -LiteralPath $OldPackageDirectory
    $envOverrides = @{
        PACKAGE_APP_NAME = $script:appName
        PACKAGE_APP_IDENTIFIER = $script:appIdentifier
        PACKAGE_APP_VERSION = $Version
        TAURI_PRODUCT_SUFFIX = "none"
        TAURI_UPDATER_PUBKEY = $PublicKey
        TAURI_UPDATER_ENDPOINTS = $Endpoint
    }
    Invoke-External `
        -FilePath $script:PowerShellHost `
        -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "tools\packaging\windows\package_tauri.ps1", "-OutputDirectory", $OldPackageDirectory) `
        -TimeoutSeconds 1800 `
        -Environment $envOverrides `
        -Name "build-old-tauri" | Out-Null

    $installer = Get-ChildItem -LiteralPath $OldPackageDirectory -Filter "*-setup.exe" -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (-not $installer) {
        throw "Old Tauri installer was not created under $OldPackageDirectory."
    }
    return $installer.FullName
}

try {
    Invoke-Stage -Name "resolve-latest-release" -ScriptBlock {
        $release = Resolve-LatestRelease
        $latestTagName = [string]$release.tagName
        $script:Summary.latestTag = $latestTagName
        $latestJsonUrl = "https://github.com/$Repo/releases/latest/download/latest.json"
        if ($LatestTag) {
            $latestJsonUrl = "https://github.com/$Repo/releases/download/$latestTagName/latest.json"
        }
        $script:Summary.latestJsonUrl = $latestJsonUrl
        "tag=$latestTagName"
    }

    Invoke-Stage -Name "download-latest-feed" -ScriptBlock {
        Remove-Tree -LiteralPath $LatestAssetsDirectory
        New-Item -ItemType Directory -Force -Path $LatestAssetsDirectory | Out-Null
        $tag = [string]$script:Summary.latestTag
        Invoke-External -FilePath "gh" -Arguments @("release", "download", $tag, "--repo", $Repo, "-p", "latest.json", "-p", "*quick-uninstall.ps1", "-D", $LatestAssetsDirectory, "--clobber") -TimeoutSeconds 180 -Name "gh-download-latest" | Out-Null
        $feed = Get-Content -LiteralPath (Join-Path $LatestAssetsDirectory "latest.json") -Raw | ConvertFrom-Json
        $latestVersion = [string]$feed.version
        $platform = $feed.platforms.'windows-x86_64'
        if (-not $platform -or -not $platform.url -or -not $platform.signature) {
            throw "latest.json does not contain a signed windows-x86_64 updater payload."
        }
        $quick = Get-ChildItem -LiteralPath $LatestAssetsDirectory -Filter "*quick-uninstall.ps1" -File | Select-Object -First 1
        if ($quick) {
            $script:quickUninstallPath = $quick.FullName
            $script:appName = Get-AssignmentValue -Path $quick.FullName -Name "AppName"
            $script:appIdentifier = Get-AssignmentValue -Path $quick.FullName -Name "AppIdentifier"
        }
        $old = if ($OldVersion) { $OldVersion } else { New-OlderPatchVersion -Version $latestVersion }
        Assert-OlderVersion -Old $old -Latest $latestVersion
        $script:Summary.latestVersion = $latestVersion
        $script:Summary.oldVersion = $old
        $script:Summary.latestInstaller = Get-AssetNameFromUrl -Url ([string]$platform.url)
        $script:Summary.appName = $script:appName
        $script:Summary.appIdentifier = $script:appIdentifier
        "latest=$latestVersion old=$old installer=$($script:Summary.latestInstaller)"
    }

    Invoke-Stage -Name "resolve-updater-key" -ScriptBlock {
        $key = Resolve-UpdaterPublicKey
        $script:Summary.updaterPublicKeyLength = $key.Length
        $script:UpdaterPublicKeyResolved = $key
        "public key available"
    }

    Invoke-Stage -Name "build-fake-old-installer" -ScriptBlock {
        $installer = Build-OldInstaller -Version ([string]$script:Summary.oldVersion) -PublicKey $script:UpdaterPublicKeyResolved -Endpoint ([string]$script:Summary.latestJsonUrl)
        $script:Summary.oldInstaller = $installer
        $installer
    }

    Invoke-Stage -Name "clean-existing-install" -ScriptBlock {
        Stop-TargetProcesses -AppName $script:appName -AppIdentifier $script:appIdentifier | Out-Null
        Invoke-QuickUninstall -QuickUninstallPath $script:quickUninstallPath | Out-Null
        foreach ($name in @($script:appName, "WGSExtract", "WGSExtract Web", "WGSExtract Windows WebUI") | Where-Object { $_ } | Select-Object -Unique) {
            if ($env:LOCALAPPDATA) {
                Remove-Tree -LiteralPath (Join-Path $env:LOCALAPPDATA $name)
            }
        }
        Stop-TargetProcesses -AppName $script:appName -AppIdentifier $script:appIdentifier
    }

    Invoke-Stage -Name "install-fake-old-version" -ScriptBlock {
        Invoke-External -FilePath ([string]$script:Summary.oldInstaller) -Arguments @("/S") -TimeoutSeconds $InstallTimeoutSeconds -Name "install-old" | Out-Null
        $installed = Resolve-InstalledApp -AppName $script:appName
        $version = Get-InstalledVersion -Executable $installed.executable
        if ($version -ne [string]$script:Summary.oldVersion) {
            throw "Expected installed old version $($script:Summary.oldVersion), got $version at $($installed.executable)."
        }
        $script:Summary.installDirectory = $installed.installDirectory
        $script:Summary.installedExecutable = $installed.executable
        "installed $version at $($installed.installDirectory)"
    }

    Invoke-Stage -Name "start-video-recording" -ScriptBlock {
        $script:videoProcess = Start-VideoRecording
        if ($script:videoProcess) { "recording $VideoPath" } else { "not requested" }
    }

    Invoke-Stage -Name "launch-and-update" -ScriptBlock {
        $autoAcceptUpdate = $true
        $actionDelay = if ($RecordVideo) { $VisiblePromptSeconds } else { 0 }
        $script:appState = Start-InstalledApp -Executable ([string]$script:Summary.installedExecutable) -InstallDirectory ([string]$script:Summary.installDirectory) -AutoUpdate $true -AutoAcceptUpdate $autoAcceptUpdate -AutoUpdateActionDelaySeconds $actionDelay
        $promptDetail = if ($RecordVideo) { "webui-auto-accepted after visible review delays" } else { "webui-auto-accepted" }
        $statuses = Wait-UpdateInstallStatus
        Assert-WebUIUpdateFlow -Statuses $statuses
        Wait-InstalledVersion -Executable ([string]$script:Summary.installedExecutable) -ExpectedVersion ([string]$script:Summary.latestVersion) | Out-Null
        $runtime = Wait-InstalledRuntimeReady -InstallDirectory ([string]$script:Summary.installDirectory)
        "port=$($script:appState.port) prompt=$promptDetail statuses=$statuses runtime=$runtime"
    }

    Invoke-Stage -Name "verify-updated-launch" -ScriptBlock {
        Stop-InstalledApp -Process $script:appState.process | Out-Null
        Stop-TargetProcesses -AppName $script:appName -AppIdentifier $script:appIdentifier | Out-Null
        $script:appState = Start-InstalledApp -Executable ([string]$script:Summary.installedExecutable) -InstallDirectory ([string]$script:Summary.installDirectory) -AutoUpdate $false
        $version = Get-InstalledVersion -Executable ([string]$script:Summary.installedExecutable)
        if ($version -ne [string]$script:Summary.latestVersion) {
            throw "Updated app launch used version $version, expected $($script:Summary.latestVersion)."
        }
        if ($RecordVideo -and $VisibleRelaunchSeconds -gt 0) {
            Start-Sleep -Seconds $VisibleRelaunchSeconds
        }
        "updated version $version launched on port $($script:appState.port)"
    }

    Invoke-Stage -Name "stop-video-recording" -ScriptBlock {
        Stop-VideoRecording -Process $script:videoProcess
        $script:videoProcess = $null
    }

    Invoke-Stage -Name "cleanup" -ScriptBlock {
        Stop-InstalledApp -Process $script:appState.process | Out-Null
        Stop-TargetProcesses -AppName $script:appName -AppIdentifier $script:appIdentifier | Out-Null
        if (-not $KeepInstalled) {
            Invoke-QuickUninstall -QuickUninstallPath $script:quickUninstallPath | Out-Null
            foreach ($name in @($script:appName, "WGSExtract", "WGSExtract Web", "WGSExtract Windows WebUI") | Where-Object { $_ } | Select-Object -Unique) {
                if ($env:LOCALAPPDATA) {
                    Remove-Tree -LiteralPath (Join-Path $env:LOCALAPPDATA $name)
                }
            }
        }
        if ($KeepInstalled) { "kept installed" } else { "removed installed app" }
    }

    Write-Summary
}
catch {
    if ($script:videoProcess) {
        try { Stop-VideoRecording -Process $script:videoProcess | Out-Null } catch { Write-Warning $_.Exception.Message }
    }
    if ($script:appState -and $script:appState.process -and -not $script:appState.process.HasExited) {
        Stop-Process -Id $script:appState.process.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-TargetProcesses -AppName $script:appName -AppIdentifier $script:appIdentifier | Out-Null
    throw
}
