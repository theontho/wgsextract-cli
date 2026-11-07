$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
$scriptsRoot = Split-Path -Parent $scriptDir
$bundleRoot = if ($env:GUI_FOR_CLI_BUNDLE_WORKSPACE) {
    $env:GUI_FOR_CLI_BUNDLE_WORKSPACE
} else {
    Split-Path -Parent $scriptsRoot
}
$runtime = Join-Path $bundleRoot "runtime\wgsextract-cli"
$manifestPath = Join-Path $runtime "install-manifest.json"

# Something that could not be removed: keep going, then fail the uninstall step,
# so the app (and its uninstaller) says so instead of reporting success.
$script:incomplete = $false
function Write-Incomplete([string]$Message) {
    Write-Warning $Message
    $script:incomplete = $true
}

function Stop-RuntimeProcesses {
    param([Parameter(Mandatory = $true)][string]$RuntimePath)

    $escapedRuntime = $RuntimePath.Replace("\", "\\")
    $processes = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and (
            $_.CommandLine -like "*$RuntimePath*" -or
            $_.CommandLine -like "*$escapedRuntime*"
        )
    })
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($processes.Count -gt 0) {
        Start-Sleep -Seconds 1
    }
}

function Remove-TreeWithRetry {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    for ($attempt = 1; $attempt -le 8; $attempt += 1) {
        if (-not (Test-Path -LiteralPath $LiteralPath)) {
            return
        }
        try {
            Get-ChildItem -LiteralPath $LiteralPath -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
                $_.Attributes = "Normal"
            }
            Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
            return
        } catch {
            Stop-RuntimeProcesses -RuntimePath $LiteralPath
            if ($attempt -eq 8) {
                throw
            }
            Start-Sleep -Milliseconds (500 * $attempt)
        }
    }
}

function Remove-UserPathEntry {
    param([Parameter(Mandatory = $true)][string]$Entry)

    # The raw value and its type: Windows' default user Path is REG_EXPAND_SZ with
    # %USERPROFILE% in it, which [Environment]::SetEnvironmentVariable would turn
    # into an expanded REG_SZ.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
    if (-not $key) {
        return $false
    }
    try {
        $userPath = $key.GetValue("Path", $null, "DoNotExpandEnvironmentNames")
        if (-not $userPath) {
            return $false
        }
        $kind = $key.GetValueKind("Path")
        $normalizedEntry = $Entry.TrimEnd("\")
        $entries = $userPath -split ";"
        $filtered = @($entries | Where-Object {
            -not $_ -or ([Environment]::ExpandEnvironmentVariables($_).TrimEnd("\") -ine $normalizedEntry)
        })
        if ($filtered.Count -eq $entries.Count) {
            return $false
        }
        $key.SetValue("Path", ($filtered -join ";"), $kind)
    } finally {
        $key.Close()
    }
    # Tell running programs (Explorer) that the environment changed, as
    # SetEnvironmentVariable does (clearing an unset variable still broadcasts).
    [Environment]::SetEnvironmentVariable("WGSEXTRACT_UNINSTALL_NOTIFY", $null, "User")
    return $true
}

function Find-MsysPacman {
    param([Parameter(Mandatory = $true)][string]$Root)
    foreach ($candidate in @(
        (Join-Path $Root "usr\bin\pacman.exe"),
        (Join-Path $Root "pacman.exe")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Test-MsysRootSafeToRemove {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not $Root) { return $false }
    try {
        $normalized = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    } catch {
        return $false
    }
    $driveRoot = [System.IO.Path]::GetPathRoot($normalized).TrimEnd("\")
    $blocked = @($driveRoot, $env:SystemRoot, $env:USERPROFILE, $env:ProgramFiles, ${env:ProgramFiles(x86)})
    foreach ($entry in $blocked) {
        if (-not $entry) { continue }
        try {
            $blockedNormalized = [System.IO.Path]::GetFullPath($entry).TrimEnd("\")
        } catch {
            continue
        }
        if ($normalized -ieq $blockedNormalized) { return $false }
    }
    # Refuse to remove anything that is not a recognizable MSYS2 root.
    $pacman = Join-Path $normalized "usr\bin\pacman.exe"
    $bash = Join-Path $normalized "usr\bin\bash.exe"
    return (Test-Path -LiteralPath $pacman -PathType Leaf) -and (Test-Path -LiteralPath $bash -PathType Leaf)
}

function Invoke-Msys2Uninstall {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        Write-Output "MSYS2 root not present, nothing to remove: $Root"
        return
    }
    if (-not (Test-MsysRootSafeToRemove -Root $Root)) {
        Write-Warning "MSYS2 root $Root failed safety checks; skipping removal."
        return
    }

    $maintenanceTool = Join-Path $Root "uninstall.exe"
    if (Test-Path -LiteralPath $maintenanceTool -PathType Leaf) {
        try {
            Write-Output "Running MSYS2 maintenance tool purge at $Root..."
            & $maintenanceTool purge --confirm-command --accept-messages 2>&1 | Out-Host
        } catch {
            Write-Warning "MSYS2 maintenance tool failed: $_"
        }
    }

    if (Test-Path -LiteralPath $Root -PathType Container) {
        Write-Output "Removing residual MSYS2 directory: $Root"
        try {
            Remove-TreeWithRetry -LiteralPath $Root
        } catch {
            Write-Incomplete "Could not fully remove MSYS2 root ${Root}: $_"
            return
        }
    }

    $uninstallKey = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\MSYS2"
    if (Test-Path -LiteralPath $uninstallKey) {
        try {
            Remove-Item -LiteralPath $uninstallKey -Recurse -Force
            Write-Output "Removed HKCU uninstall registry entry for MSYS2"
        } catch {
            Write-Incomplete "Could not remove $uninstallKey : $_"
        }
    }
    $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\MSYS2"
    if (Test-Path -LiteralPath $startMenu -PathType Container) {
        try {
            Remove-Item -LiteralPath $startMenu -Recurse -Force
            Write-Output "Removed MSYS2 Start Menu folder"
        } catch {
            Write-Incomplete "Could not remove $startMenu : $_"
        }
    }
}

function Invoke-ManifestItems {
    param([Parameter(Mandatory = $true)][string]$ManifestPath)

    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        return
    }
    try {
        $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    } catch {
        Write-Incomplete "Could not parse install manifest at ${ManifestPath}: $_"
        return
    }
    if (-not $manifest.items) {
        return
    }
    foreach ($item in $manifest.items) {
        switch ($item.type) {
            "directory" {
                if ($item.path -and (Test-Path -LiteralPath $item.path)) {
                    try {
                        Stop-RuntimeProcesses -RuntimePath $item.path
                        Remove-TreeWithRetry -LiteralPath $item.path
                        Write-Output "Removed directory: $($item.path)"
                    } catch {
                        Write-Incomplete "Could not remove $($item.path): $_"
                    }
                }
            }
            "directoryIfEmpty" {
                # Shared with other users' folders: removed only once it is empty.
                if ($item.path -and (Test-Path -LiteralPath $item.path) -and
                    -not (Get-ChildItem -LiteralPath $item.path -Force -ErrorAction SilentlyContinue)) {
                    try {
                        Remove-Item -LiteralPath $item.path -Force
                        Write-Output "Removed empty directory: $($item.path)"
                    } catch {
                        Write-Incomplete "Could not remove $($item.path): $_"
                    }
                }
            }
            "userPathEntry" {
                if ($item.path -and (Remove-UserPathEntry -Entry $item.path)) {
                    Write-Output "Removed user PATH entry: $($item.path)"
                }
            }
            "msys2Package" {
                $root = $item.msys2Root
                if (-not $root) { $root = "C:\msys64" }
                $pacman = Find-MsysPacman -Root $root
                if (-not $pacman) {
                    Write-Incomplete "Could not locate pacman.exe for MSYS2 root $root; skipping package $($item.name)."
                    continue
                }
                try {
                    & $pacman -Rs --noconfirm $item.name | Out-Host
                    if ($LASTEXITCODE -ne 0) {
                        Write-Incomplete "pacman exited $LASTEXITCODE while removing $($item.name)."
                    } else {
                        Write-Output "Removed MSYS2 package: $($item.name)"
                    }
                } catch {
                    Write-Incomplete "pacman failed for $($item.name): $_"
                }
            }
            "msys2Install" {
                $root = $item.root
                if (-not $root) { $root = "C:\msys64" }
                Invoke-Msys2Uninstall -Root $root
            }
            default {
                Write-Warning "Unknown install-manifest item type '$($item.type)'; skipping."
            }
        }
    }
}

Invoke-ManifestItems -ManifestPath $manifestPath

if (Test-Path -LiteralPath $runtime) {
    Stop-RuntimeProcesses -RuntimePath $runtime
    Remove-TreeWithRetry -LiteralPath $runtime
}
Write-Output "Removed WGS Extract runtime: $runtime"
if ($script:incomplete) {
    [Console]::Error.WriteLine("Some of what setup installed could not be removed (see the warnings above).")
    exit 1
}
