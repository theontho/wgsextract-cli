$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\python-source"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$env:PYTHON = (Get-Command python.exe).Source
$env:GIT = (Get-Command git.exe).Source
New-Item -ItemType Directory -Force -Path "$env:SRC_DIR\out" | Out-Null
$response = "$env:SRC_DIR\out\python-build.rsp"
"/p:PlatformToolset=v145`n/p:KillPython=false" | Set-Content -Encoding utf8 $response
# All upstream external inputs are prepopulated by checksum-pinned sources.
& "$env:SRC_DIR\PCbuild\build.bat" -p ARM64 -c Release --no-tkinter `
    "@$response" 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "CPython 3.13.18 ARM64 source build failed" }
& python "$env:SRC_DIR\PC\layout" --source $env:SRC_DIR `
    --build "$env:SRC_DIR\PCbuild\arm64" --arch arm64 --preset-nuget `
    --copy $env:PREFIX
if ($LASTEXITCODE) { throw "CPython ARM64 package layout failed" }
foreach ($relative in @(
    "Lib\site-packages\pip\_vendor\distlib\t32.exe",
    "Lib\site-packages\pip\_vendor\distlib\t64.exe",
    "Lib\site-packages\pip\_vendor\distlib\w32.exe",
    "Lib\site-packages\pip\_vendor\distlib\w64.exe"
)) {
    $path = Join-Path $env:PREFIX $relative
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
}
& "$env:PREFIX\python.exe" -c `
    "import sys, ssl, sqlite3, bz2, lzma, ctypes; assert sys.version_info[:3] == (3, 13, 18); print(sys.version); print(ssl.OPENSSL_VERSION)"
if ($LASTEXITCODE) { throw "Native CPython extension imports failed" }
& "$env:PREFIX\python.exe" -m pip --version
if ($LASTEXITCODE) { throw "Native CPython pip qualification failed" }
$native = Start-Process -FilePath "$env:PREFIX\python.exe" `
    -ArgumentList @("-c", '"import ssl, sqlite3, bz2, lzma, ctypes, time; time.sleep(60)"') -PassThru
try {
    & "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX `
        -ProcessIds @($native.Id) -Report "$evidence\pe.json"
} finally {
    if (-not $native.HasExited) { Stop-Process -Id $native.Id }
}
