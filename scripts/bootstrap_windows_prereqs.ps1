[CmdletBinding()]
param(
    [string]$Msys2Root = $(if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { "C:\msys64" }),
    [string]$PixiInstallUrl = $(if ($env:WGSEXTRACT_PIXI_INSTALL_URL) { $env:WGSEXTRACT_PIXI_INSTALL_URL } else { "https://pixi.sh/install.ps1" }),
    [string]$Msys2InstallerUrl = $(if ($env:WGSEXTRACT_MSYS2_INSTALLER_URL) { $env:WGSEXTRACT_MSYS2_INSTALLER_URL } else { "https://github.com/msys2/msys2-installer/releases/latest/download/msys2-base-x86_64-latest.sfx.exe" }),
    [string]$PixiInstallSha256 = $(if ($env:WGSEXTRACT_PIXI_INSTALL_SHA256) { $env:WGSEXTRACT_PIXI_INSTALL_SHA256 } else { "" }),
    [string]$Msys2InstallerSha256 = $(if ($env:WGSEXTRACT_MSYS2_INSTALLER_SHA256) { $env:WGSEXTRACT_MSYS2_INSTALLER_SHA256 } else { "" }),
    [switch]$SkipPixiInstall,
    [switch]$SkipMsys2Install
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host $Message
}

function Copy-UrlOrFile {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Source) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
        return
    }

    $invokeParams = @{
        Uri = $Source
        OutFile = $Destination
        Headers = @{ "User-Agent" = "wgsextract-cli-installer" }
    }
    if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey("UseBasicParsing")) {
        $invokeParams.UseBasicParsing = $true
    }
    Invoke-WebRequest @invokeParams
}

function Add-PathForCurrentProcess {
    param([string]$PathToAdd)

    if ((Test-Path -LiteralPath $PathToAdd) -and ($env:Path -notlike "*$PathToAdd*")) {
        $env:Path = "$PathToAdd;$env:Path"
    }
}

function Assert-FileSha256 {
    param(
        [string]$Path,
        [string]$ExpectedSha256,
        [string]$Label
    )

    if (-not $ExpectedSha256) {
        return
    }
    if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "$Label SHA-256 must be a 64-character hexadecimal digest."
    }

    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ine $ExpectedSha256) {
        throw "$Label SHA-256 mismatch. Expected $ExpectedSha256 but got $actual."
    }
    Write-Step "$Label SHA-256 verified."
}

function Ensure-Pixi {
    if ($SkipPixiInstall) {
        Write-Step "Skipping Pixi bootstrap."
        return
    }

    Add-PathForCurrentProcess (Join-Path $env:USERPROFILE ".pixi\bin")
    $pixi = Get-Command pixi -ErrorAction SilentlyContinue
    if ($pixi) {
        Write-Step "Pixi found: $($pixi.Source)"
        return
    }

    Write-Step "Pixi was not found; installing Pixi..."
    $installScript = Join-Path $env:TEMP ("wgsextract-pixi-install-{0}.ps1" -f ([guid]::NewGuid()))
    try {
        Copy-UrlOrFile -Source $PixiInstallUrl -Destination $installScript
        Assert-FileSha256 -Path $installScript -ExpectedSha256 $PixiInstallSha256 -Label "Pixi installer"
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installScript
        Add-PathForCurrentProcess (Join-Path $env:USERPROFILE ".pixi\bin")
        $pixi = Get-Command pixi -ErrorAction SilentlyContinue
        if (-not $pixi) {
            throw "Pixi installation completed, but pixi was not found on PATH."
        }
        Write-Step "Pixi installed: $($pixi.Source)"
    }
    finally {
        Remove-Item -LiteralPath $installScript -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-Msys2 {
    if ($SkipMsys2Install) {
        Write-Step "Skipping MSYS2 bootstrap."
        return
    }

    $resolvedRoot = [System.IO.Path]::GetFullPath($Msys2Root)
    $bashPath = Join-Path $resolvedRoot "usr\bin\bash.exe"
    $pacmanPath = Join-Path $resolvedRoot "usr\bin\pacman.exe"
    if ((Test-Path -LiteralPath $bashPath) -and (Test-Path -LiteralPath $pacmanPath)) {
        Write-Step "MSYS2 found: $resolvedRoot"
        return
    }

    Write-Step "MSYS2 was not found at $resolvedRoot; installing MSYS2..."
    $rootParent = Split-Path -Parent $resolvedRoot
    if (-not $rootParent) {
        throw "MSYS2 root must include a parent directory: $resolvedRoot"
    }
    if ($rootParent -and -not (Test-Path -LiteralPath $rootParent)) {
        New-Item -ItemType Directory -Path $rootParent -Force | Out-Null
    }
    $installer = Join-Path $env:TEMP ("wgsextract-msys2-install-{0}.exe" -f ([guid]::NewGuid()))
    try {
        Copy-UrlOrFile -Source $Msys2InstallerUrl -Destination $installer
        Assert-FileSha256 -Path $installer -ExpectedSha256 $Msys2InstallerSha256 -Label "MSYS2 installer"
        $isSelfExtractingArchive = $Msys2InstallerUrl -like "*.sfx.exe"
        if (-not $isSelfExtractingArchive -and (Test-Path -LiteralPath $Msys2InstallerUrl)) {
            $isSelfExtractingArchive = ([System.IO.Path]::GetFileName($Msys2InstallerUrl) -like "*.sfx.exe")
        }

        if ($isSelfExtractingArchive) {
            $extractParent = $rootParent
            $outputArg = '-o"{0}"' -f $extractParent
            $process = Start-Process -FilePath $installer -ArgumentList @(
                "-y",
                $outputArg
            ) -Wait -PassThru
            $extractedRoot = Join-Path $extractParent "msys64"
            if ($process.ExitCode -eq 0 -and $extractedRoot -ne $resolvedRoot -and (Test-Path -LiteralPath $extractedRoot)) {
                Move-Item -LiteralPath $extractedRoot -Destination $resolvedRoot -Force
            }
        }
        else {
            $rootArg = $resolvedRoot.Replace("\", "/")
            $process = Start-Process -FilePath $installer -ArgumentList @(
                "in",
                "--confirm-command",
                "--accept-messages",
                "--root",
                $rootArg
            ) -Wait -PassThru
        }
        if ($process.ExitCode -ne 0) {
            throw "MSYS2 installer failed with exit code $($process.ExitCode)."
        }
        if (-not (Test-Path -LiteralPath $bashPath)) {
            throw "MSYS2 install completed, but bash was not found at $bashPath."
        }
        if (-not (Test-Path -LiteralPath $pacmanPath)) {
            throw "MSYS2 install completed, but pacman was not found at $pacmanPath."
        }
        Write-Step "MSYS2 installed: $resolvedRoot"
    }
    finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

Ensure-Pixi
Ensure-Msys2
