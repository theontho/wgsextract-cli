@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem WGS Extract CLI installer for Windows. Like install.sh on macOS and Linux,
rem everything comes from Pixi: Python, the CLI and the native Windows builds of
rem samtools, bcftools, htslib, BWA, bwa-mem2, minimap2, fastp and samblaster.

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

set "SKIP_PIXI_INSTALL=0"
set "SKIP_PIXI_BOOTSTRAP=0"
set "SKIP_CHECKS=0"
set "DRY_RUN=0"
set "ALLOW_NONEMPTY_BOOTSTRAP_DIR=0"
set "IGNORED_ARGS="

:parse_args
if "%~1"=="" goto args_done
set "ARG=%~1"
if /I "!ARG!"=="--help" goto usage
if /I "!ARG!"=="-h" goto usage
if /I "!ARG!"=="/?" goto usage
if /I "!ARG!"=="--dry-run" (
    set "DRY_RUN=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-pixi-install" (
    set "SKIP_PIXI_INSTALL=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-pixi-bootstrap" (
    set "SKIP_PIXI_BOOTSTRAP=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-checks" (
    set "SKIP_CHECKS=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--allow-nonempty-bootstrap-dir" (
    set "ALLOW_NONEMPTY_BOOTSTRAP_DIR=1"
    shift
    goto parse_args
)
rem Options of the old MSYS2/pacman installer, which older scripts (and the
rem WGS Extract app) still pass. Its tools now come from Pixi, so these are
rem accepted and ignored.
for %%O in (--skip-msys2-install --skip-pacman-setup --skip-package-install --skip-bwa-build --skip-bwa-download --skip-minimap2-build --skip-minimap2-download --skip-samblaster-build --skip-samblaster-download --skip-fastp-build --skip-fastp-download --force-bwa-build --force-minimap2-build --force-samblaster-build --force-fastp-build) do (
    if /I "!ARG!"=="%%O" (
        set "IGNORED_ARGS=!IGNORED_ARGS! !ARG!"
        shift
        goto parse_args
    )
)
for %%O in (--msys2-root --bwa-binary-url --bwa-binary-sha256 --minimap2-binary-url --minimap2-binary-sha256 --samblaster-binary-url --samblaster-binary-sha256 --fastp-binary-url --fastp-binary-sha256) do (
    if /I "!ARG!"=="%%O" (
        set "IGNORED_ARGS=!IGNORED_ARGS! !ARG!"
        shift
        if "%~2"=="" goto missing_value
        shift
        goto parse_args
    )
)
if "!ARG:~0,2!"=="--" goto unknown_arg
rem The old installer took the MSYS2 root as a bare argument.
set "IGNORED_ARGS=!IGNORED_ARGS! !ARG!"
shift
goto parse_args

:args_done
if not defined PIXI_CACHE_DIR set "PIXI_CACHE_DIR=%CD%\tmp\pixi-cache\windows"
if not defined PIXI_PROJECT_ENVIRONMENT_DIR set "PIXI_PROJECT_ENVIRONMENT_DIR=%CD%\tmp\pixi-envs\windows"

echo --- WGS Extract CLI: Windows installer ---
echo Repository:        %CD%
echo Pixi cache:        %PIXI_CACHE_DIR%
echo Pixi envs:         %PIXI_PROJECT_ENVIRONMENT_DIR%
if defined IGNORED_ARGS echo Ignored (no longer needed; tools come from Pixi):!IGNORED_ARGS!
echo.

if "%DRY_RUN%"=="1" (
    echo Dry run only; no changes were made.
    exit /b 0
)

if not exist "%SCRIPT_DIR%pixi.toml" (
    echo No WGS Extract CLI workspace found next to install_windows.bat.
    echo Bootstrapping WGS Extract CLI source into this directory...
    call :bootstrap_source
    if errorlevel 1 exit /b 1
)

set "BOOTSTRAP_PREREQ_ARGS="
if "%SKIP_PIXI_BOOTSTRAP%"=="1" set "BOOTSTRAP_PREREQ_ARGS=-SkipPixiInstall"
echo Ensuring Pixi is installed...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\bootstrap_windows_prereqs.ps1" !BOOTSTRAP_PREREQ_ARGS!
if errorlevel 1 exit /b 1

set "PATH=%USERPROFILE%\.pixi\bin;%PATH%"
where pixi >nul 2>nul
if errorlevel 1 (
    echo ERROR: Pixi was not found on PATH.
    echo Install Pixi from https://pixi.sh and restart this terminal before rerunning.
    exit /b 1
)

if not exist "%PIXI_CACHE_DIR%" mkdir "%PIXI_CACHE_DIR%"
if errorlevel 1 exit /b 1
if not exist "%PIXI_PROJECT_ENVIRONMENT_DIR%" mkdir "%PIXI_PROJECT_ENVIRONMENT_DIR%"
if errorlevel 1 exit /b 1

echo Configuring local Pixi environment layout...
rem Store the explicit path: "true" would follow PIXI_CACHE_DIR, which is only set
rem for this session, so a later "pixi run" from a new terminal would build a
rem second copy of the environments under the default cache.
pixi config set --local detached-environments "%PIXI_PROJECT_ENVIRONMENT_DIR%"
if errorlevel 1 exit /b 1

if "%SKIP_PIXI_INSTALL%"=="0" (
    echo Installing the WGS Extract CLI and native GATK Pixi environments...
    pixi install --locked
    if errorlevel 1 exit /b 1
    pixi install --locked -e gatk
    if errorlevel 1 exit /b 1
) else (
    echo Skipping Pixi install.
)

if "%SKIP_CHECKS%"=="0" (
    echo Checking the installed tools...
    pixi run wgsextract deps check
    if errorlevel 1 exit /b 1
) else (
    echo Skipping final tool checks.
)

echo.
echo Installation complete.
echo Run: pixi run wgsextract --help
exit /b 0

:bootstrap_source
set "WGSE_REPO_URL=%WGSEXTRACT_REPO_URL%"
if not defined WGSE_REPO_URL set "WGSE_REPO_URL=https://github.com/theontho/wgsextract-cli"
set "WGSE_REQUESTED_REF=%WGSEXTRACT_REF%"
if not defined WGSE_REQUESTED_REF set "WGSE_REQUESTED_REF=%WGSEXTRACT_RELEASE_TAG%"
if not defined WGSE_REQUESTED_REF set "WGSE_REQUESTED_REF=latest"
set "WGSE_BOOTSTRAP_ARCHIVE_URL=%WGSEXTRACT_ARCHIVE_URL%"
set "WGSE_BOOTSTRAP_DIR=%SCRIPT_DIR%"
set "WGSE_BOOTSTRAP_TMP=%SCRIPT_DIR%tmp\bootstrap"
set "WGSE_ALLOW_NONEMPTY_BOOTSTRAP_DIR=%ALLOW_NONEMPTY_BOOTSTRAP_DIR%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $repoUrl = $env:WGSE_REPO_URL.TrimEnd('/'); $requestedRef = $env:WGSE_REQUESTED_REF; $archiveUrl = $env:WGSE_BOOTSTRAP_ARCHIVE_URL; $installDir = $env:WGSE_BOOTSTRAP_DIR; $tmpDir = $env:WGSE_BOOTSTRAP_TMP; $existing = @(Get-ChildItem -LiteralPath $installDir -Force | Where-Object { $_.Name -notin @('install_windows.bat', 'tmp') }); if ($existing.Count -gt 0 -and $env:WGSE_ALLOW_NONEMPTY_BOOTSTRAP_DIR -ne '1') { throw 'Install directory is not empty. Move install_windows.bat to an empty directory or rerun with --allow-nonempty-bootstrap-dir.' }; if (-not $archiveUrl) { if ($requestedRef -eq 'latest') { $apiUrl = $repoUrl -replace '^https://github\.com/', 'https://api.github.com/repos/'; $latest = Invoke-RestMethod -Uri ($apiUrl + '/releases/latest') -Headers @{ 'User-Agent' = 'wgsextract-cli-installer'; 'Accept' = 'application/vnd.github+json' }; $requestedRef = $latest.tag_name; if (-not $requestedRef) { throw 'Could not determine latest release tag.' } }; $archiveUrl = $repoUrl + '/archive/' + $requestedRef + '.zip' }; Write-Host ('Downloading WGS Extract CLI from ' + $archiveUrl); Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null; $archive = Join-Path $tmpDir 'wgsextract-cli.zip'; if (Test-Path -LiteralPath $archiveUrl) { Copy-Item -LiteralPath $archiveUrl -Destination $archive -Force } else { Invoke-WebRequest -Uri $archiveUrl -OutFile $archive -Headers @{ 'User-Agent' = 'wgsextract-cli-installer' } }; $extractDir = Join-Path $tmpDir 'source'; Expand-Archive -LiteralPath $archive -DestinationPath $extractDir -Force; $sourceDir = Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1; if (-not $sourceDir) { throw 'Downloaded archive did not contain a source directory.' }; Get-ChildItem -LiteralPath $sourceDir.FullName -Force | Where-Object { $_.Name -ne 'install_windows.bat' } | Copy-Item -Destination $installDir -Recurse -Force; if (-not (Test-Path (Join-Path $installDir 'pixi.toml'))) { throw 'Bootstrapped source did not include pixi.toml.' }; Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue; Write-Host ('Bootstrapped WGS Extract CLI into ' + $installDir)"
if errorlevel 1 exit /b 1
exit /b 0

:usage
echo WGS Extract CLI Windows installer: installs Pixi if needed, then the CLI
echo and its native Windows tools into this folder's Pixi environment.
echo.
echo Usage:
echo   install_windows.bat [options]
echo.
echo Options:
echo   --skip-pixi-install       Do not run pixi install.
echo   --skip-pixi-bootstrap     Do not install Pixi if it is missing.
echo   --skip-checks             Do not run the final wgsextract deps check.
echo   --allow-nonempty-bootstrap-dir
echo                           Allow standalone source bootstrap into a non-empty directory.
echo   --dry-run                 Print resolved paths and exit without changing anything.
echo   --help                    Show this help.
echo.
echo The old installer's MSYS2, pacman and tool-download options (--msys2-root,
echo --skip-msys2-install, --bwa-binary-url, ...) are accepted and ignored.
echo.
echo Standalone bootstrap environment variables:
echo   WGSEXTRACT_RELEASE_TAG    Release tag to download when this file is run alone. Defaults to latest.
echo   WGSEXTRACT_REF            Git ref to download when this file is run alone.
echo   WGSEXTRACT_ARCHIVE_URL    ZIP archive URL or local path to download/extract.
echo   WGSEXTRACT_PIXI_INSTALL_URL    Pixi install.ps1 URL or local path.
echo   WGSEXTRACT_PIXI_INSTALL_SHA256 Optional SHA-256 for the Pixi installer.
exit /b 0

:missing_value
echo ERROR: !ARG! requires a value.
exit /b 2

:unknown_arg
echo ERROR: Unknown option: %~1
echo Run install_windows.bat --help for usage.
exit /b 2
