# Install dependencies via WSL2 + Pixi (PowerShell Wrapper)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$bootstrapScript = Join-Path (Split-Path -Parent $scriptDir) "bootstrap_wsl.ps1"

if (Test-Path $bootstrapScript) {
    echo "Running WSL2 bootstrap script..."
    powershell -File $bootstrapScript
} else {
    echo "Error: bootstrap_wsl.ps1 not found in project root."
    exit 1
}
