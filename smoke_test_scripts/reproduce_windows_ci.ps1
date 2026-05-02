# Reproduction script for Windows CI issues
$ErrorActionPreference = "Stop"

# Ensure pixi is in path for this script
$env:Path += ";C:\Users\mac\.pixi\bin"

# Create a temporary home directory like in CI
$tmpHome = Join-Path (Get-Location) ".tmp_home"
if (Test-Path $tmpHome) { 
    Write-Host ">>> Cleaning up old .tmp_home"
    Remove-Item -Recurse -Force $tmpHome 
}
New-Item -ItemType Directory -Path $tmpHome
$env:USERPROFILE = $tmpHome

# Pixi needs a cache directory. On Windows it usually looks for AppData.
# If we override USERPROFILE, we should probably set PIXI_CACHE_DIR.
$pixiCache = Join-Path $tmpHome ".pixi_cache"
New-Item -ItemType Directory -Path $pixiCache -Force
$env:PIXI_CACHE_DIR = $pixiCache

# Mocking the config directory setup
$configDir = Join-Path $tmpHome ".config\wgsextract"
New-Item -ItemType Directory -Path $configDir -Force

Write-Host ">>> Configuring and Bootstrapping Library"
pixi run python -c "from wgsextract_cli.core.config import save_config; save_config({'reference_library': 'reference'})"
pixi run python -m wgsextract_cli.main ref bootstrap --ref reference

Write-Host ">>> Installing Reference Genome (hs38)"
pixi run python -m wgsextract_cli.main ref library --install hs38 --ref reference

Write-Host ">>> Running Basic Tests"
pixi run pytest tests/test_smoke.py
