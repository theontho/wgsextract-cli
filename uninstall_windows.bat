@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b 1

set "ASSUME_YES=0"
set "DRY_RUN=0"
set "KEEP_CONFIG=0"
set "KEEP_PIXI_ENVS=0"
set "REMOVE_PIXI_CACHE=1"
set "REMOVE_PIXI=0"
set "REMOVE_MSYS2=0"
set "REQUESTED_PREREQ_REMOVAL=0"
if defined MSYS2_ROOT (
    set "WGSE_MSYS2_ROOT=%MSYS2_ROOT%"
) else (
    set "WGSE_MSYS2_ROOT=C:\msys64"
)

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
if /I "!ARG!"=="--remove-prerequisites" (
    set "REMOVE_PIXI=1"
    set "REMOVE_MSYS2=1"
    set "REQUESTED_PREREQ_REMOVAL=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--remove-pixi" (
    set "REMOVE_PIXI=1"
    set "REQUESTED_PREREQ_REMOVAL=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--remove-msys2" (
    set "REMOVE_MSYS2=1"
    set "REQUESTED_PREREQ_REMOVAL=1"
    shift
    goto parse_args
)
if /I "!ARG!"=="--msys2-root" (
    shift
    goto parse_msys2_root
)
goto unknown_arg

:parse_msys2_root
if "%~1"=="" goto missing_msys2_root
set "WGSE_MSYS2_ROOT=%~1"
shift
goto parse_args

:args_done
set "PIXI_CACHE_DIR=%CD%\tmp\pixi-cache\windows"
set "PIXI_PROJECT_ENVIRONMENT_DIR=%CD%\tmp\pixi-envs\windows"
set "SHARED_PIXI_ENVS=%CD%\.pixi\envs"
set "PIXI_HOME=%USERPROFILE%\.pixi"
set "PIXI_BIN=%PIXI_HOME%\bin"
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
echo Pixi install:      %PIXI_HOME%
echo MSYS2 root:        %WGSE_MSYS2_ROOT%
echo.
echo This removes local project Pixi environments and clears pacman runtime defaults.
echo Pixi and MSYS2 are removed only if explicitly requested or confirmed below.
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
    if "%REQUESTED_PREREQ_REMOVAL%"=="0" (
        set /p "CONFIRM_PREREQS=Also remove Pixi and MSYS2 from this machine? [y/N] "
        if /I "!CONFIRM_PREREQS!"=="y" (
            set "REMOVE_PIXI=1"
            set "REMOVE_MSYS2=1"
        )
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

if "%REMOVE_PIXI%"=="1" (
    call :remove_dir "%PIXI_HOME%"
    if errorlevel 1 exit /b 1
    call :remove_user_path_entries "%PIXI_BIN%"
    if errorlevel 1 exit /b 1
) else (
    echo Keeping Pixi install.
)

if "%REMOVE_MSYS2%"=="1" call :validate_msys2_root "%WGSE_MSYS2_ROOT%"
if errorlevel 1 exit /b 1
if "%REMOVE_MSYS2%"=="1" (
    call :remove_dir "%WGSE_MSYS2_ROOT%"
    if errorlevel 1 exit /b 1
) else (
    echo Keeping MSYS2 install.
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

:validate_msys2_root
set "TARGET_MSYS2_ROOT=%~1"
if "%TARGET_MSYS2_ROOT%"=="" (
    echo ERROR: MSYS2 root is empty; refusing to remove it.
    exit /b 1
)
set "WGSE_TARGET_MSYS2_ROOT=%TARGET_MSYS2_ROOT%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root = [System.IO.Path]::GetFullPath($env:WGSE_TARGET_MSYS2_ROOT).TrimEnd('\'); $driveRoot = [System.IO.Path]::GetPathRoot($root).TrimEnd('\'); $programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)'); $blocked = @($driveRoot, $env:SystemRoot, $env:USERPROFILE, $env:ProgramFiles, $programFilesX86); foreach ($item in $blocked) { if ($item -and $root -ieq ([System.IO.Path]::GetFullPath($item).TrimEnd('\'))) { throw ('Refusing to remove protected directory: ' + $root) } }; $pacman = Join-Path $root 'usr\bin\pacman.exe'; $bash = Join-Path $root 'usr\bin\bash.exe'; if (-not ((Test-Path -LiteralPath $pacman) -and (Test-Path -LiteralPath $bash))) { throw ('Refusing to remove MSYS2 root without expected usr\bin\pacman.exe and usr\bin\bash.exe markers: ' + $root) }"
if errorlevel 1 exit /b 1
exit /b 0

:remove_user_path_entries
set "PATH_TO_REMOVE=%~1"
if "%PATH_TO_REMOVE%"=="" exit /b 0
set "WGSE_PATH_TO_REMOVE=%PATH_TO_REMOVE%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$remove = [System.IO.Path]::GetFullPath($env:WGSE_PATH_TO_REMOVE).TrimEnd('\'); $current = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not $current) { exit 0 }; $kept = @(); foreach ($entry in ($current -split ';')) { if (-not $entry) { continue }; try { $normalized = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($entry)).TrimEnd('\') } catch { $normalized = $entry.TrimEnd('\') }; if ($normalized -ieq $remove) { continue }; $kept += $entry }; $updated = ($kept -join ';'); if ($updated -ne $current) { [Environment]::SetEnvironmentVariable('Path', $updated, 'User'); Write-Host ('Removed from user PATH: ' + $env:WGSE_PATH_TO_REMOVE) }"
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
echo   --remove-prerequisites Remove Pixi and MSYS2 installs used by the Windows bootstrapper.
echo   --remove-pixi         Remove %%USERPROFILE%%\.pixi and its user PATH entry.
echo   --remove-msys2        Remove the MSYS2 install root.
echo   --msys2-root PATH     MSYS2 root to remove when --remove-msys2 is used. Defaults to %%MSYS2_ROOT%% or C:\msys64.
echo   --dry-run             Print resolved paths and exit without changing anything.
echo   --help                Show this help.
exit /b 0

:missing_msys2_root
echo ERROR: --msys2-root requires a path.
exit /b 2

:unknown_arg
echo ERROR: Unknown option: %~1
echo Run uninstall_windows.bat --help for usage.
exit /b 2
