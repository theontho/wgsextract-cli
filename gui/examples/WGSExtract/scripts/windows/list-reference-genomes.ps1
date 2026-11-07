# The bundle's Python scripts run with the WGS Extract runtime's Python: a fresh
# Windows has none on PATH.
& (Join-Path $PSScriptRoot "run-wgsextract-env.ps1") python (Join-Path (Split-Path -Parent $PSScriptRoot) "shared\list_reference_genomes.py") @args
exit $LASTEXITCODE
