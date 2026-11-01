$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\setuptools"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& python "$repo\packaging\windows-arm64\prepare_setuptools.py" `
    --source $env:SRC_DIR --prefix $env:PREFIX
if ($LASTEXITCODE) { throw "Native setuptools package preparation failed" }
& python -c "import sys; sys.path.insert(0, sys.argv[1]); import setuptools; assert setuptools.__version__ == '84.2.1'" `
    "$env:PREFIX\Lib\site-packages"
if ($LASTEXITCODE) { throw "Native setuptools import qualification failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
