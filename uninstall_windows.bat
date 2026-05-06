@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

set "ASSUME_YES=0"
set "DRY_RUN=0"
set "KEEP_CONFIG=0"
set "KEEP_PIXI_ENVS=0"
set "REMOVE_PIXI_CACHE=1"

:parse_args
if "%~1"=="" goto args_done
set "ARG=%~1"
if /I "!ARG!"=="--help" goto usage
if /I "!ARG!"=="-h" goto usage
if /I "!ARG!"=="/?" goto usage
if /I "!ARG!"=="--yes" (
    set "ASSUME_YES=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="-y" (
    set "ASSUME_YES=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--dry-run" (
    set "DRY_RUN=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--keep-config" (
    set "KEEP_CONFIG=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--keep-pixi-envs" (
    set "KEEP_PIXI_ENVS=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--keep-pixi-cache" (
    set "REMOVE_PIXI_CACHE=0"
    shift
    goto parse_args
)
goto unknown_arg

:args_done
set "PIXI_CACHE_DIR=%CD%\tmp\pixi-cache\windows"
set "PIXI_PROJECT_ENVIRONMENT_DIR=%CD%\tmp\pixi-envs\windows"
set "SHARED_PIXI_ENVS=%CD%\.pixi\envs"
if defined LOCALAPPDATA (
    set "CONFIG_FILE=%LOCALAPPDATA%\theontho\wgsextract\config.toml"
) else (
    set "CONFIG_FILE=%APPDATA%\wgsextract\wgsextract\config.toml"
)
set "LEGACY_CONFIG_FILE=%APPDATA%\wgsextract\wgsextract\config.toml"

echo --- WGS Extract CLI: Windows uninstaller ---
echo Repository:        %CD%
echo Pixi envs:         %PIXI_PROJECT_ENVIRONMENT_DIR%
echo Shared Pixi envs:  %SHARED_PIXI_ENVS%
echo Pixi cache:        %PIXI_CACHE_DIR%
echo Config file:       %CONFIG_FILE%
echo Legacy config:     %LEGACY_CONFIG_FILE%
echo.
echo This removes local project Pixi environments and clears pacman runtime defaults.
echo It does not uninstall Pixi, MSYS2, or MSYS2 packages.
echo.

if "%DRY_RUN%"=="1" (
    echo Dry run only; no changes were made.
    exit /b 0
)

if "%ASSUME_YES%"=="0" (
    set /p "CONFIRM=Continue? [y/N] "
    if /I not "!CONFIRM!"=="y" (
        echo Uninstall cancelled.
        exit /b 0
    )
)

if "%KEEP_CONFIG%"=="0" (
    call :clear_pacman_config "%CONFIG_FILE%"
    if errorlevel 1 exit /b 1
    if /I not "%LEGACY_CONFIG_FILE%"=="%CONFIG_FILE%" (
        call :clear_pacman_config "%LEGACY_CONFIG_FILE%"
        if errorlevel 1 exit /b 1
    )
) else (
    echo Keeping WGS Extract config unchanged.
)

if "%KEEP_PIXI_ENVS%"=="0" (
    call :remove_dir "%PIXI_PROJECT_ENVIRONMENT_DIR%"
    if errorlevel 1 exit /b 1
    call :remove_dir "%SHARED_PIXI_ENVS%"
    if errorlevel 1 exit /b 1
) else (
    echo Keeping Pixi environments.
)

if "%REMOVE_PIXI_CACHE%"=="1" (
    call :remove_dir "%PIXI_CACHE_DIR%"
    if errorlevel 1 exit /b 1
) else (
    echo Keeping Pixi cache.
)

echo.
echo Uninstall complete.
exit /b 0

:clear_pacman_config
set "TARGET_CONFIG=%~1"
if "%TARGET_CONFIG%"=="" exit /b 0
if not exist "%TARGET_CONFIG%" (
    echo Config file not found; nothing to clear: %TARGET_CONFIG%
    exit /b 0
)
set "CONFIG_TMP=%TARGET_CONFIG%.tmp"
set "CONFIG_FILE_TO_CLEAR=%TARGET_CONFIG%"
set "CONFIG_TMP_TO_WRITE=%CONFIG_TMP%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$configPath = $env:CONFIG_FILE_TO_CLEAR; $tempPath = $env:CONFIG_TMP_TO_WRITE; $pattern = '^\s*(tool_runtime|pacman_ucrt64_bin)\s*='; $lines = @(Get-Content -LiteralPath $configPath | Where-Object { $_ -notmatch $pattern }); $encoding = New-Object System.Text.UTF8Encoding($false); [System.IO.File]::WriteAllLines($tempPath, [string[]]$lines, $encoding)"
if errorlevel 1 exit /b 1
move /y "%CONFIG_TMP%" "%TARGET_CONFIG%" >nul
if errorlevel 1 exit /b 1
echo Cleared pacman runtime defaults from config: %TARGET_CONFIG%
exit /b 0

:remove_dir
set "TARGET_DIR=%~1"
if not exist "%TARGET_DIR%" (
    echo Not found: %TARGET_DIR%
    exit /b 0
)
echo Removing: %TARGET_DIR%
rmdir /s /q "%TARGET_DIR%"
if errorlevel 1 exit /b 1
exit /b 0

:usage
echo WGS Extract CLI Windows uninstaller for the batch pacman setup.
echo.
echo Usage:
echo   uninstall_windows.bat [options]
echo.
echo Options:
echo   --yes, -y             Do not prompt for confirmation.
echo   --keep-config         Leave WGS Extract config.toml unchanged.
echo   --keep-pixi-envs      Leave local Pixi environments in place.
echo   --keep-pixi-cache     Leave the local Windows Pixi cache in place.
echo   --dry-run             Print resolved paths and exit without changing anything.
echo   --help                Show this help.
exit /b 0

:unknown_arg
echo ERROR: Unknown option: %~1
echo Run uninstall_windows.bat --help for usage.
exit /b 2
