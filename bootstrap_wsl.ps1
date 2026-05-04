# WSL2 + Pixi Bootstrap for WGS Extract CLI

param(
    [switch]$Tune,
    [string]$Memory,
    [int]$Processors,
    [string]$Swap
)

$ErrorActionPreference = "Stop"

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
    Write-Host "Updated WSL resource settings at $configPath" -ForegroundColor Green
    Write-Host "Defaults use host ratios: processors=2/3, memory=3/4, swap=1/4." -ForegroundColor Cyan
    Write-Host "Resolved settings: memory=$($settings['memory']), processors=$($settings['processors']), swap=$($settings['swap']) (host: $($defaults['hostProcessors']) CPUs, $($defaults['hostMemoryGb'])GB RAM)" -ForegroundColor Cyan
    Write-Host "Run 'wsl --shutdown' or reboot Windows for these settings to take effect." -ForegroundColor Yellow
}

function Check-WSL {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        Write-Host "WSL not found. Attempting to install..." -ForegroundColor Red
        try {
            Start-Process powershell -ArgumentList "wsl --install" -Verb RunAs -Wait
        } catch {
            Write-Host "Failed to launch WSL installation. Please run 'wsl --install' manually." -ForegroundColor Red
            exit 1
        }
        Write-Host "WSL installation has been initiated. You MUST REBOOT." -ForegroundColor Yellow
        exit 1
    }
}

function Install-Ubuntu {
    Write-Host "Checking for Ubuntu in WSL..." -ForegroundColor Cyan
    $distros = wsl -l -q | Out-String
    $cleanDistros = $distros.Replace("`0", "").Replace(" ", "").Replace("`n", "").Replace("`r", "")
    
    if ($cleanDistros -notmatch "Ubuntu") {
        Write-Host "Ubuntu not found. Installing..." -ForegroundColor Cyan
        wsl --install -d Ubuntu
    } else {
        Write-Host "Ubuntu is already installed." -ForegroundColor Green
    }
}

function Setup-Pixi {
    Write-Host "Setting up Pixi inside WSL..." -ForegroundColor Cyan
    $installCmd = "curl -fsSL https://pixi.sh/install.sh | bash"
    wsl bash -c $installCmd
    
    $checkCmd = "~/.pixi/bin/pixi --version"
    $pixi_check = wsl bash -c $checkCmd
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Pixi is installed: $pixi_check" -ForegroundColor Green
    } else {
        Write-Host "Pixi installation failed." -ForegroundColor Red
        exit 1
    }
}

function Setup-Repo {
    # Convert Windows path to use forward slashes to avoid escape issues in WSL
    $winPath = (Get-Location).Path.Replace('\', '/')
    Write-Host "Converting Windows path: $winPath" -ForegroundColor Cyan
    
    $wslPathRaw = wsl wslpath -u "$winPath"
    # Clean up output
    $wslPath = ([string]$wslPathRaw).Replace("`0", "").Replace("`n", "").Replace("`r", "").Trim()
    
    if (-not $wslPath) {
        Write-Host "Failed to convert path to WSL." -ForegroundColor Red
        exit 1
    }
    
    Write-Host "Current repository path in WSL: $wslPath" -ForegroundColor Cyan
    Write-Host "Initializing project dependencies in WSL..." -ForegroundColor Cyan
    
    $initCmd = "cd '$wslPath' && ~/.pixi/bin/pixi install"
    wsl bash -c $initCmd
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Project initialized in WSL." -ForegroundColor Green
    } else {
        Write-Host "Failed to initialize project in WSL." -ForegroundColor Red
        exit 1
    }
}

Write-Host "--- WGS Extract CLI: WSL2 Bootstrap ---" -ForegroundColor Blue
Write-Host "For native Windows + WSL setup, use .\setup_windows.ps1 instead." -ForegroundColor Yellow
Update-WSLConfig
Check-WSL
Install-Ubuntu
Setup-Pixi
Setup-Repo

Write-Host "`nSetup complete!" -ForegroundColor Green
$winPath = (Get-Location).Path.Replace('\', '/')
$currentWslPath = wsl wslpath -u "$winPath"
$currentWslPath = ([string]$currentWslPath).Replace("`0", "").Replace("`n", "").Replace("`r", "").Trim()
Write-Host "To run the CLI: wsl bash -c 'cd ""$currentWslPath"" && ~/.pixi/bin/pixi run wgsextract --help'" -ForegroundColor Cyan




