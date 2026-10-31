# Installs Pixi for the current user when it's missing: the one prerequisite of
# install_windows.bat. Every tool, Python included, then comes from Pixi.
[CmdletBinding()]
param(
    [string]$PixiInstallUrl = $(if ($env:WGSEXTRACT_PIXI_INSTALL_URL) { $env:WGSEXTRACT_PIXI_INSTALL_URL } else { "" }),
    [string]$PixiInstallSha256 = $(if ($env:WGSEXTRACT_PIXI_INSTALL_SHA256) { $env:WGSEXTRACT_PIXI_INSTALL_SHA256 } else { "" }),
    [switch]$SkipPixiInstall
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

    $releaseAsset = Resolve-GitHubReleaseAsset -Source $Source
    if ($releaseAsset) {
        try {
            Copy-GitHubReleaseAsset -Asset $releaseAsset -Destination $Destination
            return
        }
        catch {
            Write-Warning "GitHub API release-asset download failed for ${Source}: $($_.Exception.Message). Falling back to direct URL."
        }
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

function Resolve-GitHubReleaseAsset {
    param([string]$Source)

    try {
        $uri = [System.Uri]$Source
    }
    catch {
        return $null
    }
    if ($uri.Host -ne "github.com") {
        return $null
    }

    $path = $uri.AbsolutePath.Trim("/")
    $taggedMatch = [regex]::Match($path, "^([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$")
    $latestMatch = [regex]::Match($path, "^([^/]+)/([^/]+)/releases/latest/download/(.+)$")
    if ($taggedMatch.Success) {
        return [pscustomobject]@{
            ApiUrl = "https://api.github.com/repos/$([System.Uri]::UnescapeDataString($taggedMatch.Groups[1].Value))/$([System.Uri]::UnescapeDataString($taggedMatch.Groups[2].Value))/releases/tags/$([System.Uri]::UnescapeDataString($taggedMatch.Groups[3].Value))"
            AssetName = [System.Uri]::UnescapeDataString($taggedMatch.Groups[4].Value)
        }
    }
    if ($latestMatch.Success) {
        return [pscustomobject]@{
            ApiUrl = "https://api.github.com/repos/$([System.Uri]::UnescapeDataString($latestMatch.Groups[1].Value))/$([System.Uri]::UnescapeDataString($latestMatch.Groups[2].Value))/releases/latest"
            AssetName = [System.Uri]::UnescapeDataString($latestMatch.Groups[3].Value)
        }
    }
    return $null
}

function Copy-GitHubReleaseAsset {
    param(
        [Parameter(Mandatory = $true)]$Asset,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $headers = @{
        Accept = "application/vnd.github+json"
        "User-Agent" = "wgsextract-cli-installer"
    }
    if ($env:GITHUB_TOKEN) {
        $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN"
    }

    $release = $null
    for ($attempt = 1; $attempt -le 5; $attempt += 1) {
        try {
            $release = Invoke-RestMethod -UseBasicParsing -Uri $Asset.ApiUrl -Headers $headers -TimeoutSec 60
            break
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Seconds (5 * $attempt)
        }
    }

    $releaseAsset = $release.assets | Where-Object { $_.name -eq $Asset.AssetName } | Select-Object -First 1
    if (-not $releaseAsset) {
        throw "Release $($release.tag_name) does not include asset $($Asset.AssetName)."
    }

    $assetHeaders = @{
        Accept = "application/octet-stream"
        "User-Agent" = "wgsextract-cli-installer"
    }
    if ($env:GITHUB_TOKEN) {
        $assetHeaders["Authorization"] = "Bearer $env:GITHUB_TOKEN"
    }

    for ($attempt = 1; $attempt -le 5; $attempt += 1) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $releaseAsset.url -Headers $assetHeaders -OutFile $Destination -TimeoutSec 600
            Write-Step "Downloaded $($Asset.AssetName) from GitHub Releases API release $($release.tag_name)."
            return
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
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
        throw "$Label requires a trusted SHA-256 digest."
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
    if ($env:PIXI_VERSION -and $env:PIXI_VERSION -notin @("v0.82.0", "0.82.0")) {
        throw "No trusted Pixi checksums for $env:PIXI_VERSION; use v0.82.0."
    }
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    switch ($arch) {
        "AMD64" {
            $asset = "pixi-x86_64-pc-windows-msvc.exe"
            $expected = "89c7455923dac401c1853310894d87fa06602911b838e389a46c7bc8df00171e"
        }
        "ARM64" {
            $asset = "pixi-aarch64-pc-windows-msvc.exe"
            $expected = "d205d144618bdc83e25dba1a10c8cff8f39e9da85c06e142a9ec0d0c8f6d3559"
        }
        default { throw "Unsupported Pixi architecture: $arch" }
    }
    $url = if ($PixiInstallUrl) { $PixiInstallUrl } else { "https://github.com/prefix-dev/pixi/releases/download/v0.82.0/$asset" }
    if ($PixiInstallSha256) { $expected = $PixiInstallSha256 }
    $bin = Join-Path $env:USERPROFILE ".pixi\bin"
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    $download = Join-Path $bin ("pixi-download-{0}.exe" -f ([guid]::NewGuid()))
    try {
        Copy-UrlOrFile -Source $url -Destination $download
        Assert-FileSha256 -Path $download -ExpectedSha256 $expected -Label "Pixi executable"
        Move-Item -LiteralPath $download -Destination (Join-Path $bin "pixi.exe") -Force
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $entries = @($userPath -split ";" | ForEach-Object {
            [Environment]::ExpandEnvironmentVariables($_).Trim().TrimEnd('\')
        })
        if ($entries -notcontains $bin.TrimEnd('\')) {
            [Environment]::SetEnvironmentVariable("Path", "$bin;" + $userPath, "User")
        }
        Add-PathForCurrentProcess $bin
        $pixi = Get-Command pixi -ErrorAction SilentlyContinue
        if (-not $pixi) {
            throw "Pixi installation completed, but pixi was not found on PATH."
        }
        Write-Step "Pixi installed: $($pixi.Source)"
    }
    finally {
        Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
    }
}

Ensure-Pixi
