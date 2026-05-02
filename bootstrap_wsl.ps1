# WSL2 + Pixi Bootstrap for WGS Extract CLI

$ErrorActionPreference = "Stop"

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
Check-WSL
Install-Ubuntu
Setup-Pixi
Setup-Repo

Write-Host "`nSetup complete!" -ForegroundColor Green
$winPath = (Get-Location).Path.Replace('\', '/')
$currentWslPath = wsl wslpath -u "$winPath"
$currentWslPath = ([string]$currentWslPath).Replace("`0", "").Replace("`n", "").Replace("`r", "").Trim()
Write-Host "To run the CLI: wsl bash -c 'cd ""$currentWslPath"" && ~/.pixi/bin/pixi run wgsextract --help'" -ForegroundColor Cyan
