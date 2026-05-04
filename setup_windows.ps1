# Windows + WSL2 setup for WGS Extract CLI

param(
    [string]$Distribution = "Ubuntu",
    [switch]$SkipWindowsPixi,
    [switch]$SkipWsl,
    [switch]$SkipPixiInstall,
    [switch]$SkipChecks,
    [switch]$Tune,
    [string]$Memory,
    [int]$Processors,
    [string]$Swap,
    [string]$PixiCacheRoot = "tmp/pixi-cache",
    [string]$PixiEnvRoot = "tmp/pixi-envs"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n>>> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Yellow
}

function Fail {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
    exit 1
}

function Add-PathForCurrentProcess {
    param([string]$PathToAdd)
    if ((Test-Path $PathToAdd) -and ($env:Path -notlike "*$PathToAdd*")) {
        $env:Path = "$PathToAdd;$env:Path"
    }
}

function Initialize-WindowsPixiLocations {
    $cachePath = Join-Path (Get-Location) (Join-Path $PixiCacheRoot "windows")
    $envPath = Join-Path (Get-Location) (Join-Path $PixiEnvRoot "windows")
    New-Item -ItemType Directory -Path $cachePath -Force | Out-Null
    New-Item -ItemType Directory -Path $envPath -Force | Out-Null
    $env:PIXI_CACHE_DIR = $cachePath
    $env:PIXI_PROJECT_ENVIRONMENT_DIR = $envPath
    Write-Ok "Using Windows pixi cache: $cachePath"
    Write-Ok "Using Windows pixi environments: $envPath"
}

function Invoke-Checked {
    param(
        [string]$Description,
        [string[]]$Command
    )

    Write-Step $Description
    $exe = $Command[0]
    $args = @($Command | Select-Object -Skip 1)
    & $exe @args
    if ($LASTEXITCODE -ne 0) {
        Fail "Command failed: $($Command -join ' ')"
    }
}

function Get-DefaultWSLSettings {
    $computer = Get-CimInstance Win32_ComputerSystem
    $hostProcessors = [int]$computer.NumberOfLogicalProcessors
    $hostMemoryGb = [Math]::Round($computer.TotalPhysicalMemory / 1GB)

    return @{
        memory = "$([Math]::Ceiling($hostMemoryGb * 0.75))GB"
        processors = [string]([Math]::Max(1, [Math]::Round($hostProcessors * 2 / 3)))
        swap = "$([Math]::Ceiling($hostMemoryGb * 0.25))GB"
        hostProcessors = $hostProcessors
        hostMemoryGb = $hostMemoryGb
    }
}
function Update-WSLConfig {
    if (-not $Tune) {
        return
    }

    $defaults = Get-DefaultWSLSettings
    $settings = @{}
    $settings["memory"] = if ($Memory) { $Memory } else { $defaults["memory"] }
    $settings["processors"] = if ($Processors -gt 0) { [string]$Processors } else { $defaults["processors"] }
    $settings["swap"] = if ($Swap) { $Swap } else { $defaults["swap"] }

    $configPath = Join-Path $env:USERPROFILE ".wslconfig"
    $lines = @()
    if (Test-Path $configPath) {
        $lines = Get-Content $configPath
    }

    $output = New-Object System.Collections.Generic.List[string]
    $inWsl2 = $false
    $sawWsl2 = $false
    $applied = @{}

    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith("[") -and $trimmed.EndsWith("]")) {
            if ($inWsl2) {
                foreach ($key in $settings.Keys) {
                    if (-not $applied.ContainsKey($key)) {
                        $output.Add("$key=$($settings[$key])")
                        $applied[$key] = $true
                    }
                }
            }
            $inWsl2 = $trimmed.ToLowerInvariant() -eq "[wsl2]"
            if ($inWsl2) { $sawWsl2 = $true }
            $output.Add($line)
            continue
        }

        if ($inWsl2 -and $trimmed.Contains("=")) {
            $key = $trimmed.Split("=", 2)[0].Trim().ToLowerInvariant()
            if ($settings.ContainsKey($key)) {
                $output.Add("$key=$($settings[$key])")
                $applied[$key] = $true
                continue
            }
        }
        $output.Add($line)
    }

    if ($inWsl2) {
        foreach ($key in $settings.Keys) {
            if (-not $applied.ContainsKey($key)) {
                $output.Add("$key=$($settings[$key])")
                $applied[$key] = $true
            }
        }
    }

    if (-not $sawWsl2) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1].Trim()) {
            $output.Add("")
        }
        $output.Add("[wsl2]")
        foreach ($key in $settings.Keys) {
            $output.Add("$key=$($settings[$key])")
        }
    }

    Set-Content -Path $configPath -Value $output -Encoding UTF8
    Write-Ok "Updated WSL resource settings at $configPath"
    Write-Ok "Defaults use host ratios: processors=2/3, memory=3/4, swap=1/4."
    Write-Ok "Resolved settings: memory=$($settings['memory']), processors=$($settings['processors']), swap=$($settings['swap']) (host: $($defaults['hostProcessors']) CPUs, $($defaults['hostMemoryGb'])GB RAM)"
    Write-Warn "Run 'wsl --shutdown' or reboot Windows for these settings to take effect."
}

function Ensure-WindowsPixi {
    if ($SkipWindowsPixi) {
        Write-Warn "Skipping native Windows pixi setup."
        return
    }

    Write-Step "Checking native Windows pixi"
    Add-PathForCurrentProcess (Join-Path $env:USERPROFILE ".pixi\bin")
    $pixi = Get-Command pixi -ErrorAction SilentlyContinue
    if ($pixi) {
        Write-Ok "Windows pixi found: $($pixi.Source)"
        pixi --version
        return
    }

    Write-Step "Installing native Windows pixi"
    $installScript = Join-Path $env:TEMP "pixi-install.ps1"
    Invoke-WebRequest -Uri "https://pixi.sh/install.ps1" -OutFile $installScript
    powershell -NoProfile -ExecutionPolicy Bypass -File $installScript
    Add-PathForCurrentProcess (Join-Path $env:USERPROFILE ".pixi\bin")

    $pixi = Get-Command pixi -ErrorAction SilentlyContinue
    if (-not $pixi) {
        Fail "Windows pixi install completed, but pixi is still not on PATH. Restart PowerShell and rerun this script."
    }
    Write-Ok "Windows pixi installed: $($pixi.Source)"
}

function Ensure-WSL {
    if ($SkipWsl) {
        Write-Warn "Skipping WSL setup."
        return
    }

    Write-Step "Checking WSL"
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        Write-Warn "WSL command was not found. Launching WSL install as administrator."
        Start-Process powershell -ArgumentList "wsl --install -d $Distribution" -Verb RunAs -Wait
        Write-Warn "WSL installation may require a reboot. Rerun this script after rebooting."
        exit 1
    }

    $distros = (wsl -l -q | Out-String).Replace("`0", "")
    if ($distros -notmatch [regex]::Escape($Distribution)) {
        Write-Step "Installing WSL distribution: $Distribution"
        wsl --install -d $Distribution
        if ($LASTEXITCODE -ne 0) {
            Fail "Failed to install WSL distribution: $Distribution"
        }
        Write-Warn "If WSL asks for first-time Linux user setup, complete it and rerun this script."
    } else {
        Write-Ok "WSL distribution found: $Distribution"
    }
}

function Invoke-WslBash {
    param([string]$Script)
    wsl -d $Distribution bash -lc $Script
    if ($LASTEXITCODE -ne 0) {
        Fail "WSL command failed: $Script"
    }
}

function Ensure-WslPixi {
    if ($SkipWsl) {
        return
    }

    Write-Step "Checking WSL pixi"
    wsl -d $Distribution bash -lc "test -x ~/.pixi/bin/pixi && ~/.pixi/bin/pixi --version"
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "WSL pixi is available."
        return
    }

    Write-Step "Installing pixi inside WSL"
    Invoke-WslBash "command -v curl >/dev/null 2>&1 || (sudo apt-get update && sudo apt-get install -y curl ca-certificates)"
    Invoke-WslBash "curl -fsSL https://pixi.sh/install.sh | bash"
    Invoke-WslBash "~/.pixi/bin/pixi --version"
    Write-Ok "WSL pixi installed."
}

function Get-WslRepoPath {
    $winPath = (Get-Location).Path.Replace('\', '/')
    $wslPathRaw = wsl -d $Distribution wslpath -a -u "$winPath"
    $wslPath = ([string]$wslPathRaw).Replace("`0", "").Replace("`n", "").Replace("`r", "").Trim()
    if (-not $wslPath) {
        Fail "Failed to translate repository path to WSL path."
    }
    return $wslPath
}

function Get-WslHomePath {
    $homeRaw = wsl -d $Distribution bash -lc 'printf %s "$HOME"'
    $wslHome = ([string]$homeRaw).Replace("`0", "").Replace("`n", "").Replace("`r", "").Trim()
    if (-not $wslHome) {
        Fail "Failed to determine WSL home directory."
    }
    return $wslHome
}

function Get-WslPixiCachePath {
    $wslHome = Get-WslHomePath
    return "$wslHome/.cache/wgsextract-cli/pixi-cache"
}

function Get-WslPixiEnvPath {
    $wslHome = Get-WslHomePath
    return "$wslHome/.cache/wgsextract-cli/pixi-envs"
}

function Invoke-WindowsPixiInstall {
    Write-Step "Installing native Windows pixi environment"
    pixi config set --local detached-environments true
    pixi install
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Warn "pixi install failed. Cleaning generated Windows pixi environment and retrying once."
    pixi clean cache --conda --yes
    $envDir = $env:PIXI_PROJECT_ENVIRONMENT_DIR
    if ($envDir -and (Test-Path $envDir)) {
        Remove-Item -Recurse -Force $envDir
        New-Item -ItemType Directory -Path $envDir -Force | Out-Null
    }
    $sharedEnvDir = Join-Path (Get-Location) ".pixi\envs"
    if (Test-Path $sharedEnvDir) {
        Remove-Item -Recurse -Force $sharedEnvDir
    }

    pixi config set --local detached-environments true
    pixi install
    if ($LASTEXITCODE -ne 0) {
        Fail "Command failed after repair retry: pixi install"
    }
}

function Install-ProjectEnvironments {
    if ($SkipPixiInstall) {
        Write-Warn "Skipping pixi install in both runtimes."
        return
    }

    if (-not $SkipWindowsPixi) {
        Invoke-WindowsPixiInstall
    }

    if (-not $SkipWsl) {
        $wslPath = Get-WslRepoPath
        $wslCache = Get-WslPixiCachePath
        $wslEnv = Get-WslPixiEnvPath
        Write-Step "Installing WSL pixi environment at $wslPath"
        Invoke-WslBash "mkdir -p $(Quote-Bash $wslCache) $(Quote-Bash $wslEnv) && export PIXI_CACHE_DIR=$(Quote-Bash $wslCache) PIXI_PROJECT_ENVIRONMENT_DIR=$(Quote-Bash $wslEnv) && cd $(Quote-Bash $wslPath) && ~/.pixi/bin/pixi config set --local detached-environments true && ~/.pixi/bin/pixi install || (~/.pixi/bin/pixi clean cache --conda --yes || true; rm -rf $(Quote-Bash $wslEnv) .pixi/envs; mkdir -p $(Quote-Bash $wslEnv); ~/.pixi/bin/pixi config set --local detached-environments true; ~/.pixi/bin/pixi install)"
    }
}

function Quote-Bash {
    param([string]$Value)
    return "'" + $Value.Replace("'", "'\''") + "'"
}

function Run-SetupChecks {
    if ($SkipChecks) {
        Write-Warn "Skipping final setup checks."
        return
    }

    if (-not $SkipWindowsPixi) {
        Write-Step "Checking native CLI dependency view"
        $env:WGSEXTRACT_TOOL_RUNTIME = "wsl"
        pixi run python -m wgsextract_cli.main deps wsl check
        if ($LASTEXITCODE -ne 0) {
            Fail "Windows-hosted WSL runtime check failed."
        }
    }

    if (-not $SkipWsl) {
        $wslPath = Get-WslRepoPath
        $wslCache = Get-WslPixiCachePath
        $wslEnv = Get-WslPixiEnvPath
        Write-Step "Checking WSL CLI dependency view"
        Invoke-WslBash "mkdir -p $(Quote-Bash $wslCache) $(Quote-Bash $wslEnv) && export PIXI_CACHE_DIR=$(Quote-Bash $wslCache) PIXI_PROJECT_ENVIRONMENT_DIR=$(Quote-Bash $wslEnv) && cd $(Quote-Bash $wslPath) && ~/.pixi/bin/pixi run python -m wgsextract_cli.main deps check"
    }
}

Write-Host "--- WGS Extract CLI: Windows + WSL Setup ---" -ForegroundColor Blue
Update-WSLConfig
Initialize-WindowsPixiLocations
Ensure-WindowsPixi
Ensure-WSL
Ensure-WslPixi
Install-ProjectEnvironments
Run-SetupChecks

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host "Native CLI: pixi run wgsextract --help" -ForegroundColor Cyan
if (-not $SkipWsl) {
    $finalWslPath = Get-WslRepoPath
    $quotedFinalWslPath = Quote-Bash $finalWslPath
    Write-Host "WSL CLI:    wsl -d $Distribution bash -lc `"cd $quotedFinalWslPath && ~/.pixi/bin/pixi run wgsextract --help`"" -ForegroundColor Cyan
}




