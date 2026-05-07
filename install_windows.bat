@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

if defined MSYS2_ROOT (
    set "WGSE_MSYS2_ROOT=%MSYS2_ROOT%"
) else (
    set "WGSE_MSYS2_ROOT=C:\msys64"
)
set "SKIP_PIXI_INSTALL=0"
set "SKIP_PACMAN_SETUP=0"
set "SKIP_PACKAGE_INSTALL=0"
set "SKIP_BWA_BUILD=0"
set "SKIP_BWA_DOWNLOAD=0"
set "FORCE_BWA_BUILD=0"
set "BWA_BINARY_URL="
set "SKIP_PIXI_BOOTSTRAP=0"
set "SKIP_MSYS2_INSTALL=0"
set "SKIP_CHECKS=0"
set "DRY_RUN=0"
set "ALLOW_NONEMPTY_BOOTSTRAP_DIR=0"

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
if /I "!ARG!"=="--skip-msys2-install" (
    set "SKIP_MSYS2_INSTALL=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-pacman-setup" (
    set "SKIP_PACMAN_SETUP=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-package-install" (
    set "SKIP_PACKAGE_INSTALL=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-bwa-build" (
    set "SKIP_BWA_BUILD=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--skip-bwa-download" (
    set "SKIP_BWA_DOWNLOAD=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--bwa-binary-url" (
    shift
    if "%~1"=="" goto missing_bwa_binary_url
    set "BWA_BINARY_URL=%~1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--force-bwa-build" (
    set "FORCE_BWA_BUILD=1"
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
if /I "!ARG!"=="--msys2-root" (
    shift
    if "%~1"=="" goto missing_msys2_root
    set "WGSE_MSYS2_ROOT=%~1"
    shift
    goto parse_args
)
if "!ARG:~0,2!"=="--" goto unknown_arg
set "WGSE_MSYS2_ROOT=%~1"
shift
goto parse_args

:args_done
set "WGSE_UCRT64_BIN=%WGSE_MSYS2_ROOT%\ucrt64\bin"
set "PIXI_CACHE_DIR=%CD%\tmp\pixi-cache\windows"
set "PIXI_PROJECT_ENVIRONMENT_DIR=%CD%\tmp\pixi-envs\windows"
set "WGSEXTRACT_TOOL_RUNTIME=pacman"
set "WGSEXTRACT_PACMAN_UCRT64_BIN=%WGSE_UCRT64_BIN%"

echo --- WGS Extract CLI: Windows pacman installer ---
echo Repository:        %CD%
echo MSYS2 root:        %WGSE_MSYS2_ROOT%
echo UCRT64 bin:        %WGSE_UCRT64_BIN%
echo Pixi cache:        %PIXI_CACHE_DIR%
echo Pixi envs:         %PIXI_PROJECT_ENVIRONMENT_DIR%
echo Default runtime:   pacman
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
if "%SKIP_PIXI_BOOTSTRAP%"=="1" set "BOOTSTRAP_PREREQ_ARGS=!BOOTSTRAP_PREREQ_ARGS! -SkipPixiInstall"
if "%SKIP_MSYS2_INSTALL%"=="1" set "BOOTSTRAP_PREREQ_ARGS=!BOOTSTRAP_PREREQ_ARGS! -SkipMsys2Install"
echo Ensuring Windows prerequisites are installed...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\bootstrap_windows_prereqs.ps1" -Msys2Root "%WGSE_MSYS2_ROOT%" !BOOTSTRAP_PREREQ_ARGS!
if errorlevel 1 exit /b 1
set "PATH=%USERPROFILE%\.pixi\bin;%PATH%"

where pixi >nul 2>nul
if errorlevel 1 (
    echo ERROR: Pixi was not found on PATH.
    echo Install Pixi from https://pixi.sh and restart this terminal before rerunning.
    exit /b 1
)

if not exist "%WGSE_MSYS2_ROOT%\usr\bin\bash.exe" (
    echo ERROR: MSYS2 bash was not found at "%WGSE_MSYS2_ROOT%\usr\bin\bash.exe".
    echo Install MSYS2 from https://www.msys2.org/ or pass --msys2-root PATH.
    exit /b 1
)
if not exist "%WGSE_MSYS2_ROOT%\usr\bin\pacman.exe" (
    echo ERROR: MSYS2 pacman was not found at "%WGSE_MSYS2_ROOT%\usr\bin\pacman.exe".
    echo Install MSYS2 from https://www.msys2.org/ or pass --msys2-root PATH.
    exit /b 1
)

if not exist "%PIXI_CACHE_DIR%" mkdir "%PIXI_CACHE_DIR%"
if errorlevel 1 exit /b 1
if not exist "%PIXI_PROJECT_ENVIRONMENT_DIR%" mkdir "%PIXI_PROJECT_ENVIRONMENT_DIR%"
if errorlevel 1 exit /b 1

echo Configuring local Pixi environment layout...
pixi config set --local detached-environments true
if errorlevel 1 exit /b 1

if "%SKIP_PIXI_INSTALL%"=="0" (
    echo Installing WGS Extract CLI Pixi environment...
    pixi install
    if errorlevel 1 exit /b 1
) else (
    echo Skipping Pixi install.
)

if "%SKIP_PACMAN_SETUP%"=="0" (
    echo Setting up MSYS2 UCRT64 pacman runtime tools...
    set "PACMAN_SETUP_EXTRA_ARGS="
    if "%SKIP_PACKAGE_INSTALL%"=="1" set "PACMAN_SETUP_EXTRA_ARGS=!PACMAN_SETUP_EXTRA_ARGS! -SkipPackageInstall"
    if "%SKIP_BWA_BUILD%"=="1" set "PACMAN_SETUP_EXTRA_ARGS=!PACMAN_SETUP_EXTRA_ARGS! -SkipBwaBuild"
    if "%SKIP_BWA_DOWNLOAD%"=="1" set "PACMAN_SETUP_EXTRA_ARGS=!PACMAN_SETUP_EXTRA_ARGS! -SkipBwaDownload"
    if "%FORCE_BWA_BUILD%"=="1" set "PACMAN_SETUP_EXTRA_ARGS=!PACMAN_SETUP_EXTRA_ARGS! -ForceBwaBuild"
    if "%BWA_BINARY_URL%"=="" (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\setup_pacman_runtime.ps1" -Msys2Root "%WGSE_MSYS2_ROOT%" !PACMAN_SETUP_EXTRA_ARGS!
    ) else (
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\setup_pacman_runtime.ps1" -Msys2Root "%WGSE_MSYS2_ROOT%" !PACMAN_SETUP_EXTRA_ARGS! -BwaBinaryUrl "%BWA_BINARY_URL%"
    )
    if errorlevel 1 exit /b 1
) else (
    echo Skipping pacman runtime setup.
)

echo Persisting pacman as the default WGS Extract runtime...
pixi run python -c "import os; from wgsextract_cli.core.config import get_config_path, save_config; save_config({'tool_runtime':'pacman','pacman_ucrt64_bin':os.environ['WGSEXTRACT_PACMAN_UCRT64_BIN']}); print(get_config_path())"
if errorlevel 1 exit /b 1

if "%SKIP_CHECKS%"=="0" (
    echo Validating pacman runtime through wgsextract...
    pixi run wgsextract deps pacman check
    if errorlevel 1 exit /b 1
) else (
    echo Skipping final runtime checks.
)

echo.
echo Installation complete.
echo Run: pixi run wgsextract --help
echo Runtime defaults were set to pacman in the WGS Extract config file.
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
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $repoUrl = $env:WGSE_REPO_URL.TrimEnd('/'); $requestedRef = $env:WGSE_REQUESTED_REF; $archiveUrl = $env:WGSE_BOOTSTRAP_ARCHIVE_URL; $installDir = $env:WGSE_BOOTSTRAP_DIR; $tmpDir = $env:WGSE_BOOTSTRAP_TMP; $existing = @(Get-ChildItem -LiteralPath $installDir -Force | Where-Object { $_.Name -notin @('install_windows.bat', 'tmp') }); if ($existing.Count -gt 0 -and $env:WGSE_ALLOW_NONEMPTY_BOOTSTRAP_DIR -ne '1') { throw 'Install directory is not empty. Move install_windows.bat to an empty directory or rerun with --allow-nonempty-bootstrap-dir.' }; if (-not $archiveUrl) { if ($requestedRef -eq 'latest') { $latest = Invoke-RestMethod -Uri ($repoUrl + '/releases/latest') -Headers @{ 'User-Agent' = 'wgsextract-cli-installer' }; $requestedRef = $latest.tag_name; if (-not $requestedRef) { throw 'Could not determine latest release tag.' } }; $archiveUrl = $repoUrl + '/archive/' + $requestedRef + '.zip' }; Write-Host ('Downloading WGS Extract CLI from ' + $archiveUrl); Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null; $archive = Join-Path $tmpDir 'wgsextract-cli.zip'; if (Test-Path -LiteralPath $archiveUrl) { Copy-Item -LiteralPath $archiveUrl -Destination $archive -Force } else { Invoke-WebRequest -Uri $archiveUrl -OutFile $archive -Headers @{ 'User-Agent' = 'wgsextract-cli-installer' } }; $extractDir = Join-Path $tmpDir 'source'; Expand-Archive -LiteralPath $archive -DestinationPath $extractDir -Force; $sourceDir = Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1; if (-not $sourceDir) { throw 'Downloaded archive did not contain a source directory.' }; Get-ChildItem -LiteralPath $sourceDir.FullName -Force | Where-Object { $_.Name -ne 'install_windows.bat' } | Copy-Item -Destination $installDir -Recurse -Force; if (-not (Test-Path (Join-Path $installDir 'pixi.toml'))) { throw 'Bootstrapped source did not include pixi.toml.' }; Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue; Write-Host ('Bootstrapped WGS Extract CLI into ' + $installDir)"
if errorlevel 1 exit /b 1
exit /b 0

:usage
echo WGS Extract CLI Windows installer, defaulting external tools to pacman.
echo.
echo Usage:
echo   install_windows.bat [--msys2-root PATH] [options]
echo   install_windows.bat C:\msys64 [options]
echo.
echo Options:
echo   --msys2-root PATH          MSYS2 install root. Defaults to %%MSYS2_ROOT%% or C:\msys64.
echo   --skip-pixi-install       Do not run pixi install.
echo   --skip-pixi-bootstrap     Do not install Pixi if it is missing.
echo   --skip-msys2-install      Do not install MSYS2 if it is missing.
echo   --skip-pacman-setup       Do not run scripts\setup_pacman_runtime.ps1.
echo   --skip-package-install    Pass -SkipPackageInstall to the pacman setup helper.
echo   --skip-bwa-build          Pass -SkipBwaBuild to the pacman setup helper.
echo   --skip-bwa-download       Build BWA locally instead of downloading the release binary.
echo   --bwa-binary-url URL      Download BWA from this ZIP URL or local ZIP path.
echo   --force-bwa-build         Pass -ForceBwaBuild to the pacman setup helper.
echo   --skip-checks             Do not run the final wgsextract pacman dependency check.
echo   --allow-nonempty-bootstrap-dir
echo                           Allow standalone source bootstrap into a non-empty directory.
echo   --dry-run                 Print resolved paths and exit without changing anything.
echo   --help                    Show this help.
echo.
echo Standalone bootstrap environment variables:
echo   WGSEXTRACT_RELEASE_TAG    Release tag to download when this file is run alone. Defaults to latest.
echo   WGSEXTRACT_REF            Git ref to download when this file is run alone.
echo   WGSEXTRACT_ARCHIVE_URL    ZIP archive URL or local path to download/extract.
echo   WGSEXTRACT_PIXI_INSTALL_URL    Pixi install.ps1 URL or local path.
echo   WGSEXTRACT_PIXI_INSTALL_SHA256 Optional SHA-256 for the Pixi installer.
echo   WGSEXTRACT_MSYS2_INSTALLER_URL MSYS2 installer URL or local path.
echo   WGSEXTRACT_MSYS2_INSTALLER_SHA256 Optional SHA-256 for the MSYS2 installer.
exit /b 0

:missing_msys2_root
echo ERROR: --msys2-root requires a path.
exit /b 2

:missing_bwa_binary_url
echo ERROR: --bwa-binary-url requires a URL or path.
exit /b 2

:unknown_arg
echo ERROR: Unknown option: %~1
echo Run install_windows.bat --help for usage.
exit /b 2
