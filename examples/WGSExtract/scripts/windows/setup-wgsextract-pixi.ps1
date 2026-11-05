$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repoUrl = if ($env:WGSEXTRACT_REPO_URL) { $env:WGSEXTRACT_REPO_URL } else { "https://github.com/WGSExtract/wgsextract-cli" }
$scriptDir = Split-Path -Parent $PSCommandPath
$scriptsRoot = Split-Path -Parent $scriptDir
$defaultReleaseTagFile = if ($env:WGSEXTRACT_RELEASE_TAG_FILE) { $env:WGSEXTRACT_RELEASE_TAG_FILE } else { Join-Path $scriptsRoot "wgsextract-release-tag.txt" }
$defaultReleaseTag = if ($env:WGSEXTRACT_DEFAULT_RELEASE_TAG) {
    $env:WGSEXTRACT_DEFAULT_RELEASE_TAG
} elseif (Test-Path -LiteralPath $defaultReleaseTagFile -PathType Leaf) {
    $tag = Get-Content -LiteralPath $defaultReleaseTagFile -TotalCount 1
    if ($tag) { $tag.Trim() } else { "" }
} else {
    ""
}
if (-not $defaultReleaseTag) { $defaultReleaseTag = "latest" }
$requestedRef = if ($env:WGSEXTRACT_REF) { $env:WGSEXTRACT_REF } elseif ($env:WGSEXTRACT_RELEASE_TAG) { $env:WGSEXTRACT_RELEASE_TAG } else { $defaultReleaseTag }
$bundleRoot = if ($env:GUI_FOR_CLI_BUNDLE_WORKSPACE) { $env:GUI_FOR_CLI_BUNDLE_WORKSPACE } else { Split-Path -Parent $scriptsRoot }
$installDir = if ($env:WGSEXTRACT_INSTALL_DIR) { $env:WGSEXTRACT_INSTALL_DIR } else { Join-Path $bundleRoot "runtime\wgsextract-cli" }
$appDir = Join-Path $installDir "app"
$binDir = Join-Path $installDir "bin"

. (Join-Path $scriptDir "wgsextract-msys2.ps1")
$msys2Root = Get-WGSExtractMsys2Root

function Test-MsysInstall {
    param([Parameter(Mandatory = $true)][string]$Root)
    return (Test-Path -LiteralPath (Join-Path $Root "usr\bin\pacman.exe") -PathType Leaf) -and
           (Test-Path -LiteralPath (Join-Path $Root "usr\bin\bash.exe") -PathType Leaf)
}

function Find-Pixi {
    if ($env:PIXI -and (Test-Path -LiteralPath $env:PIXI -PathType Leaf)) { return $env:PIXI }
    $command = Get-Command pixi -ErrorAction SilentlyContinue
    if ($command -and $command.Path) { return $command.Path }
    if ($command -and $command.Source) { return $command.Source }
    $homePixi = Join-Path $HOME ".pixi\bin\pixi.exe"
    if (Test-Path -LiteralPath $homePixi -PathType Leaf) { return $homePixi }
    return $null
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Value, $utf8NoBom)
}

function Remove-DirectoryWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Attempts = 8
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    for ($i = 1; $i -le $Attempts; $i += 1) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($i -eq $Attempts) { throw }
            Start-Sleep -Milliseconds (500 * $i)
        }
    }
}

function Restore-AppSource {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$AppDir
    )

    # A complete extract has both install_windows.bat and pixi.toml at the
    # root. If only install_windows.bat is present, a previous run was
    # interrupted mid-extract — re-extract instead of trusting the partial
    # state. We also clean up any stale .new sibling and any leftover
    # app.new subdirectory left under a previous partial extract.
    $alreadyExtracted = (Test-Path -LiteralPath (Join-Path $AppDir "install_windows.bat") -PathType Leaf) -and
                        (Test-Path -LiteralPath (Join-Path $AppDir "pixi.toml") -PathType Leaf)
    if ($alreadyExtracted) {
        $staleNew = Join-Path $AppDir "app.new"
        Remove-DirectoryWithRetry -Path $staleNew
        return
    }

    $newAppDir = "$AppDir.new"
    Remove-DirectoryWithRetry -Path $newAppDir
    # The .zip, as the CLI's own Windows installer uses: the release has
    # symlinks, which tar.exe can't create without Developer Mode ("Invalid
    # argument") and Expand-Archive writes as small files. It is staged in
    # %TEMP%, a short path, as Expand-Archive can't write paths over 260 characters.
    $staging = Join-Path ([System.IO.Path]::GetTempPath()) ("wgsextract-" + [guid]::NewGuid().ToString("N"))
    Expand-Archive -LiteralPath $Archive -DestinationPath $staging -Force
    $top = @(Get-ChildItem -LiteralPath $staging -Directory)
    if ($top.Count -ne 1) {
        Write-Error "Downloaded archive did not contain a single source directory."
        exit 1
    }
    Move-Item -LiteralPath $top[0].FullName -Destination $newAppDir
    Remove-DirectoryWithRetry -Path $staging
    if (-not (Test-Path -LiteralPath (Join-Path $newAppDir "install_windows.bat") -PathType Leaf)) {
        Write-Error "Downloaded archive did not contain install_windows.bat."
        exit 1
    }
    if (-not (Test-Path -LiteralPath (Join-Path $newAppDir "pixi.toml") -PathType Leaf)) {
        Write-Error "Downloaded archive did not contain pixi.toml."
        exit 1
    }
    if (Test-Path -LiteralPath $AppDir) { Remove-DirectoryWithRetry -Path $AppDir }

    # Antivirus (Defender) sometimes briefly holds files inside the
    # freshly-extracted directory. Retry Move-Item a few times before giving up.
    $attempts = 6
    for ($i = 1; $i -le $attempts; $i += 1) {
        try {
            Move-Item -LiteralPath $newAppDir -Destination $AppDir -ErrorAction Stop
            return
        }
        catch {
            if ($i -eq $attempts) { throw }
            Start-Sleep -Milliseconds (500 * $i)
        }
    }
}

# Pre-state snapshot so the manifest records who owns each prerequisite.
$msys2PreInstalled = Test-MsysInstall -Root $msys2Root
$pixiPreInstalled = ($null -ne (Find-Pixi))
# The app's own folder for MSYS2 when %LOCALAPPDATA% can't hold it (see wgsextract-msys2.ps1).
$userFolder = Get-WGSExtractUserFolder
$msys2InUserFolder = $msys2Root.StartsWith("$userFolder\", [System.StringComparison]::OrdinalIgnoreCase)

if ($env:WGSEXTRACT_ARCHIVE_URL) {
    $archiveUrls = @($env:WGSEXTRACT_ARCHIVE_URL)
} else {
    if ($requestedRef -eq "latest" -or -not $requestedRef) {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$repoUrl/releases/latest" -MaximumRedirection 0 -ErrorAction SilentlyContinue
        $ref = if ($response.Headers.Location) { Split-Path $response.Headers.Location -Leaf } else { "main" }
    } else {
        $ref = $requestedRef
    }
    # Build a fallback list. github.com/.../archive/$ref.zip is the
    # canonical URL but it intermittently returns 504 from the github.com
    # edge while the underlying codeload CDN remains healthy. Always include
    # codeload as a fallback so flaky edge outages don't break setup.
    $archiveUrls = @("$repoUrl/archive/$ref.zip")
    if ($repoUrl -match '^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$') {
        $owner = $Matches[1]
        $repo = $Matches[2]
        if ($ref -like 'v*' -or $ref -match '^\d') {
            $archiveUrls += "https://codeload.github.com/$owner/$repo/zip/refs/tags/$ref"
        }
        $archiveUrls += "https://codeload.github.com/$owner/$repo/zip/refs/heads/$ref"
        $archiveUrls += "https://codeload.github.com/$owner/$repo/zip/$ref"
    }
}

New-Item -ItemType Directory -Force -Path $installDir, (Join-Path $installDir "tmp"), $binDir | Out-Null
$workDir = Join-Path (Join-Path $installDir "tmp") ("install." + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
$archive = Join-Path $workDir "wgsextract-cli.zip"

try {
    $downloadAttempts = 5
    $downloaded = $false
    foreach ($currentUrl in $archiveUrls) {
        Write-Host "Downloading WGS Extract CLI from $currentUrl"
        for ($attempt = 1; $attempt -le $downloadAttempts; $attempt += 1) {
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $currentUrl -OutFile $archive -TimeoutSec 120
                $downloaded = $true
                break
            }
            catch {
                $message = $_.Exception.Message
                if ($attempt -eq $downloadAttempts) {
                    Write-Host "All $downloadAttempts attempts failed for $currentUrl ($message). Trying next fallback URL if any..."
                    break
                }
                $delay = [int][math]::Min(60, [math]::Pow(2, $attempt))
                Write-Host "Download attempt $attempt/$downloadAttempts failed ($message). Retrying in $delay s..."
                Start-Sleep -Seconds $delay
            }
        }
        if ($downloaded) { break }
    }
    if (-not $downloaded) {
        throw "Failed to download WGS Extract CLI from any of: $($archiveUrls -join ', ')"
    }
    Restore-AppSource -Archive $archive -AppDir $appDir

    $installBat = Join-Path $appDir "install_windows.bat"
    if (-not (Test-Path -LiteralPath $installBat -PathType Leaf)) {
        Write-Error "Upstream install_windows.bat not found at $installBat. Tarball layout changed?"
        exit 1
    }

    # Redirect Pixi cache+envs to a short machine-local path to avoid Windows MAX_PATH
    # (260) issues when nested under the workspace. Tracked in install-manifest so the
    # uninstaller removes them.
    $pixiBaseDir = if ($env:WGSEXTRACT_PIXI_BASE_DIR) { $env:WGSEXTRACT_PIXI_BASE_DIR } else { Join-Path $env:LOCALAPPDATA "WGSExtractPixi" }
    $pixiCacheDirNew = Join-Path $pixiBaseDir "cache"
    $pixiEnvsDirNew = Join-Path $pixiBaseDir "envs"
    New-Item -ItemType Directory -Force -Path $pixiCacheDirNew, $pixiEnvsDirNew | Out-Null
    $env:PIXI_CACHE_DIR = $pixiCacheDirNew
    $env:PIXI_PROJECT_ENVIRONMENT_DIR = $pixiEnvsDirNew

    # CLI releases before native Windows tools (0.6.x and older) install MSYS2
    # and its pacman tools; newer ones take every tool from Pixi and need none.
    $usesMsys2 = Test-Path -LiteralPath (Join-Path $appDir "scripts\setup_pacman_runtime.ps1") -PathType Leaf
    if ($usesMsys2) {
        if ($msys2InUserFolder) {
            New-WGSExtractUserFolder | Out-Null
        }
        $env:MSYS2_ROOT = $msys2Root
    } else {
        $msys2InUserFolder = $false
    }
    $batArgs = @()

    # install_windows.bat saves its runtime defaults in the user's own CLI config
    # (platformdirs' user_config_dir("wgsextract", "theontho")). The app's commands
    # use the bundle's config and find MSYS2 through MSYS2_ROOT, so that file is put
    # back as it was: a user's own CLI settings stay theirs, and none is left behind.
    $userCliConfig = Join-Path $env:LOCALAPPDATA "theontho\wgsextract\config.toml"
    $userCliConfigBefore = if (Test-Path -LiteralPath $userCliConfig -PathType Leaf) { [System.IO.File]::ReadAllBytes($userCliConfig) } else { $null }

    Write-Host "Delegating to upstream install_windows.bat (installs Pixi as needed and the CLI's tools)..."
    Push-Location $appDir
    try {
        & $env:ComSpec /d /c "call `"$installBat`""
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        Pop-Location
        if ($null -ne $userCliConfigBefore) {
            [System.IO.File]::WriteAllBytes($userCliConfig, $userCliConfigBefore)
        } elseif (Test-Path -LiteralPath $userCliConfig) {
            Remove-Item -LiteralPath $userCliConfig -Force
            foreach ($dir in (Split-Path -Parent $userCliConfig), (Join-Path $env:LOCALAPPDATA "theontho")) {
                if ((Test-Path -LiteralPath $dir) -and -not (Get-ChildItem -LiteralPath $dir -Force)) {
                    Remove-Item -LiteralPath $dir -Force
                }
            }
        }
    }

    if ($usesMsys2 -and -not (Test-MsysInstall -Root $msys2Root)) {
        Write-Error "MSYS2 was not present at $msys2Root after install_windows.bat completed."
        exit 1
    }
    $pixi = Find-Pixi
    if (-not $pixi) {
        Write-Error "Pixi was not found on PATH or in ~\.pixi after install_windows.bat completed."
        exit 1
    }

    $shimPath = Join-Path $binDir "wgsextract.cmd"
    $runScript = Join-Path $scriptDir "run-wgsextract.ps1"
    Write-Utf8File -LiteralPath $shimPath -Value "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File ""$runScript"" %*`r`n"

    $manifestItems = New-Object System.Collections.Generic.List[object]
    if (-not $pixiPreInstalled) {
        $pixiBinDir = Split-Path -Parent $pixi
        $pixiInstallRoot = Split-Path -Parent $pixiBinDir
        if ($pixiInstallRoot) {
            $manifestItems.Add(@{ type = "directory"; path = $pixiInstallRoot; ownedBy = "install_windows.bat" })
        }
        if ($pixiBinDir) {
            $manifestItems.Add(@{ type = "userPathEntry"; path = $pixiBinDir; ownedBy = "install_windows.bat" })
        }
    }
    if ($usesMsys2 -and -not $msys2PreInstalled) {
        $manifestItems.Add(@{ type = "msys2Install"; root = $msys2Root; ownedBy = "install_windows.bat" })
    }
    if ($msys2InUserFolder) {
        # Only the app makes this folder; it and, once no other user's is left, its parent go.
        $manifestItems.Add(@{ type = "directory"; path = $userFolder; ownedBy = "setup-wgsextract-pixi.ps1" })
        $manifestItems.Add(@{ type = "directoryIfEmpty"; path = (Split-Path -Parent $userFolder); ownedBy = "setup-wgsextract-pixi.ps1" })
    }
    if (Test-Path -LiteralPath $pixiBaseDir) {
        $manifestItems.Add(@{ type = "directory"; path = $pixiBaseDir; ownedBy = "setup-wgsextract-pixi.ps1" })
    }
    # A rerun of setup finds what the first run installed already there; it is
    # still the app's to remove.
    $manifestPath = Join-Path $installDir "install-manifest.json"
    if (Test-Path -LiteralPath $manifestPath) {
        $previous = @((Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json).items)
        foreach ($item in $previous) {
            $key = "$($item.type)|$($item.path)|$($item.root)"
            if (-not ($manifestItems | Where-Object { "$($_.type)|$($_.path)|$($_.root)" -eq $key })) {
                $copy = @{}
                foreach ($property in $item.PSObject.Properties) { $copy[$property.Name] = $property.Value }
                $manifestItems.Add($copy)
            }
        }
    }

    # Uninstall goes in this order: MSYS2 (with its own uninstaller) before the
    # folders that hold it.
    $order = @("msys2Package", "msys2Install", "userPathEntry", "directory", "directoryIfEmpty")
    $manifest = @{
        format = 1
        createdAt = (Get-Date).ToUniversalTime().ToString("o")
        wgsextractRef = $requestedRef
        items = @(foreach ($type in $order) { $manifestItems | Where-Object { $_.type -eq $type } })
    }
    Set-Content -LiteralPath $manifestPath -Encoding UTF8 -Value ($manifest | ConvertTo-Json -Depth 5)

    Write-Host "WGS Extract CLI is installed in $installDir"
    exit 0
} finally {
    if (Test-Path -LiteralPath $workDir) {
        try {
            Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Warning "Failed to remove temporary setup directory ${workDir}: $($_.Exception.Message)"
        }
    }
}
