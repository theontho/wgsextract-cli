param(
    [string]$OutputDirectory = "out\release\tauri"
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $repoRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $utf8NoBom)
}

function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function Resolve-RepoChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$BasePath,
        [Parameter(Mandatory = $true)][string]$ChildPath
    )

    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath)
    $combinedPath = if ([System.IO.Path]::IsPathRooted($ChildPath)) {
        $ChildPath
    } else {
        Join-Path $baseFullPath $ChildPath
    }
    $fullPath = [System.IO.Path]::GetFullPath($combinedPath)
    $basePrefix = $baseFullPath.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar

    if ($fullPath -eq $baseFullPath -or -not $fullPath.StartsWith($basePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "OutputDirectory must resolve inside the repository root. Got '$ChildPath' -> '$fullPath'; expected child of '$baseFullPath'."
    }

    return $fullPath
}

function New-QuickUninstallScript {
    param(
        [Parameter(Mandatory = $true)][string]$InstallerName,
        [Parameter(Mandatory = $true)][string]$AppName,
        [Parameter(Mandatory = $true)][string]$AppVersion,
        [Parameter(Mandatory = $true)][string]$AppIdentifier
    )

    $installerFileName = [System.IO.Path]::GetFileName($InstallerName)
    if (-not $installerFileName -or [System.IO.Path]::IsPathRooted($installerFileName) -or $installerFileName.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0) {
        throw "InstallerName must be a file name, not a path: $InstallerName"
    }

    $scriptName = $installerFileName -replace "-setup\.exe$", "-quick-uninstall.ps1"
    if ($scriptName -eq $installerFileName) {
        $scriptName = "$([System.IO.Path]::GetFileNameWithoutExtension($installerFileName))-quick-uninstall.ps1"
    }

    $appNameLiteral = ConvertTo-PowerShellLiteral -Value $AppName
    $appVersionLiteral = ConvertTo-PowerShellLiteral -Value $AppVersion
    $appIdentifierLiteral = ConvertTo-PowerShellLiteral -Value $AppIdentifier
    $installerNameLiteral = ConvertTo-PowerShellLiteral -Value $installerFileName

    $script = @"
`$ErrorActionPreference = "Stop"

`$AppName = $appNameLiteral
`$AppVersion = $appVersionLiteral
`$AppIdentifier = $appIdentifierLiteral
`$InstallerName = $installerNameLiteral

function Remove-Tree {
    param([Parameter(Mandatory = `$true)][string]`$LiteralPath)

    if (-not `$LiteralPath -or -not (Test-Path -LiteralPath `$LiteralPath)) {
        return
    }
    for (`$attempt = 1; `$attempt -le 6; `$attempt += 1) {
        try {
            Remove-Item -LiteralPath `$LiteralPath -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if (-not (Test-Path -LiteralPath `$LiteralPath)) {
                return
            }
            if (`$attempt -eq 6) {
                throw
            }
            Start-Sleep -Milliseconds (250 * `$attempt)
        }
    }
}

function Stop-WGSExtractProcesses {
    `$needles = @(
        `$AppName,
        `$AppIdentifier,
        "gui-for-cli-webui-tauri",
        "BundleWorkspaces\wgs-extract",
        "BundleWorkspaces/wgs-extract"
    ) | Where-Object { `$_ }

    `$currentPid = `$PID
    Get-CimInstance Win32_Process | Where-Object {
        `$processCommandLine = `$_.CommandLine
        `$_.ProcessId -ne `$currentPid -and `$processCommandLine -and (`$needles | Where-Object { `$_.Length -gt 0 -and `$processCommandLine -like "*`$_*" })
    } | ForEach-Object {
        Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-SilentUninstaller {
    param([string]`$InstallDirectory)

    `$uninstaller = Join-Path `$InstallDirectory "uninstall.exe"
    if (-not (Test-Path -LiteralPath `$uninstaller -PathType Leaf)) {
        return `$false
    }

    `$process = Start-Process -FilePath `$uninstaller -ArgumentList @("/S") -Wait -PassThru -WindowStyle Hidden
    if (`$process.ExitCode -ne 0) {
        throw "Silent uninstaller failed with exit code `$(`$process.ExitCode): `$uninstaller"
    }
    return `$true
}

function Get-ApplicationSupportRoots {
    `$roots = @()
    if (`$env:XDG_DATA_HOME) {
        `$roots += `$env:XDG_DATA_HOME
    }
    if (`$HOME) {
        `$roots += (Join-Path `$HOME ".local\share")
    }
    if (`$env:LOCALAPPDATA) {
        `$roots += `$env:LOCALAPPDATA
    }
    return `$roots | Where-Object { `$_ } | Select-Object -Unique
}

function Remove-InstallerShortcuts {
    `$shortcutRoots = @()
    if (`$env:APPDATA) {
        `$shortcutRoots += (Join-Path `$env:APPDATA "Microsoft\Windows\Start Menu\Programs")
    }
    if (`$env:PUBLIC) {
        `$shortcutRoots += (Join-Path `$env:PUBLIC "Desktop")
    }
    if (`$HOME) {
        `$shortcutRoots += (Join-Path `$HOME "Desktop")
    }

    `$shortcutNames = @(`$AppName, "`$AppName Web") | Select-Object -Unique
    foreach (`$root in (`$shortcutRoots | Where-Object { `$_ } | Select-Object -Unique)) {
        foreach (`$name in `$shortcutNames) {
            Remove-Tree -LiteralPath (Join-Path `$root "`$name.lnk")
            Remove-Tree -LiteralPath (Join-Path `$root `$name)
        }
    }
}

Write-Host "Quick-uninstalling `$AppName `$AppVersion installed by `$InstallerName"
Stop-WGSExtractProcesses

`$installDirectories = @()
if (`$env:LOCALAPPDATA) {
    `$installDirectories += (Join-Path `$env:LOCALAPPDATA `$AppName)
    if (`$AppName -notlike "* Web") {
        `$installDirectories += (Join-Path `$env:LOCALAPPDATA "`$AppName Web")
    }
}

foreach (`$installDirectory in (`$installDirectories | Select-Object -Unique)) {
    Invoke-SilentUninstaller -InstallDirectory `$installDirectory | Out-Null
}

Stop-WGSExtractProcesses
Remove-InstallerShortcuts

`$dataDirectories = @()
foreach (`$root in Get-ApplicationSupportRoots) {
    `$dataDirectories += (Join-Path `$root `$AppIdentifier)
}
if (`$env:LOCALAPPDATA) {
    `$dataDirectories += (Join-Path `$env:LOCALAPPDATA `$AppName)
    if (`$AppName -notlike "* Web") {
        `$dataDirectories += (Join-Path `$env:LOCALAPPDATA "`$AppName Web")
    }
}

foreach (`$path in ((`$installDirectories + `$dataDirectories) | Where-Object { `$_ } | Select-Object -Unique)) {
    Remove-Tree -LiteralPath `$path
}

Write-Host "Quick uninstall complete."
"@

    return [ordered]@{
        name = $scriptName
        contents = $script
    }
}

Invoke-Checked -FilePath npm -Arguments @("--prefix", "app", "run", "tauri:dist")

$bundleRoot = Join-Path $PWD "app\web\packagers\tauri\target\release\bundle"
$brandingPath = Join-Path $PWD "app\web\packagers\tauri\target\release\branding.json"
$outputRoot = Resolve-RepoChildPath -BasePath $repoRoot -ChildPath $OutputDirectory
if (Test-Path -LiteralPath $outputRoot) {
    Remove-Item -LiteralPath $outputRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$patterns = @(
    "nsis\*.exe"
)
$optionalPatterns = @(
    "nsis\*.exe.sig"
)
$copied = @()
foreach ($pattern in $patterns) {
    Get-ChildItem -Path (Join-Path $bundleRoot $pattern) -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $outputRoot $_.Name) -Force
        $copied += $_.Name
    }
}
foreach ($pattern in $optionalPatterns) {
    Get-ChildItem -Path (Join-Path $bundleRoot $pattern) -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $outputRoot $_.Name) -Force
        $copied += $_.Name
    }
}

if ($copied.Count -eq 0) {
    throw "No Tauri distribution artifacts were found under $bundleRoot"
}

if (-not (Test-Path -LiteralPath $brandingPath -PathType Leaf)) {
    throw "Tauri branding metadata was not found: $brandingPath"
}
$branding = Get-Content -LiteralPath $brandingPath -Raw | ConvertFrom-Json
$installerArtifacts = @($copied | Where-Object { $_ -like "*-setup.exe" })
foreach ($installer in $installerArtifacts) {
    $quickUninstall = New-QuickUninstallScript `
        -InstallerName $installer `
        -AppName ([string]$branding.appName) `
        -AppVersion ([string]$branding.appVersion) `
        -AppIdentifier ([string]$branding.appIdentifier)
    Write-Utf8File -LiteralPath (Join-Path $outputRoot $quickUninstall.name) -Value $quickUninstall.contents
    $copied += $quickUninstall.name
}

$manifest = [ordered]@{
    platform = "windows"
    outputDirectory = $OutputDirectory
    artifacts = $copied
}
Write-Utf8File -LiteralPath (Join-Path $outputRoot "tauri-package.json") -Value (($manifest | ConvertTo-Json -Depth 4) + "`n")
Write-Output ((Resolve-Path $outputRoot).Path)
