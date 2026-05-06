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
set "SKIP_CHECKS=0"
set "DRY_RUN=0"

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
    set "PACMAN_SETUP_ARGS=-NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\setup_pacman_runtime.ps1" -Msys2Root "%WGSE_MSYS2_ROOT%""
    if "%SKIP_PACKAGE_INSTALL%"=="1" set "PACMAN_SETUP_ARGS=!PACMAN_SETUP_ARGS! -SkipPackageInstall"
    if "%SKIP_BWA_BUILD%"=="1" set "PACMAN_SETUP_ARGS=!PACMAN_SETUP_ARGS! -SkipBwaBuild"
    if "%SKIP_BWA_DOWNLOAD%"=="1" set "PACMAN_SETUP_ARGS=!PACMAN_SETUP_ARGS! -SkipBwaDownload"
    if defined BWA_BINARY_URL set "PACMAN_SETUP_ARGS=!PACMAN_SETUP_ARGS! -BwaBinaryUrl "!BWA_BINARY_URL!""
    if "%FORCE_BWA_BUILD%"=="1" set "PACMAN_SETUP_ARGS=!PACMAN_SETUP_ARGS! -ForceBwaBuild"
    powershell.exe !PACMAN_SETUP_ARGS!
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
echo   --skip-pacman-setup       Do not run scripts\setup_pacman_runtime.ps1.
echo   --skip-package-install    Pass -SkipPackageInstall to the pacman setup helper.
echo   --skip-bwa-build          Pass -SkipBwaBuild to the pacman setup helper.
echo   --skip-bwa-download       Build BWA locally instead of downloading the release binary.
echo   --bwa-binary-url URL      Download BWA from this ZIP URL or local ZIP path.
echo   --force-bwa-build         Pass -ForceBwaBuild to the pacman setup helper.
echo   --skip-checks             Do not run the final wgsextract pacman dependency check.
echo   --dry-run                 Print resolved paths and exit without changing anything.
echo   --help                    Show this help.
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
