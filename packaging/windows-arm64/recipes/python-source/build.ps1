$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\python-source"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$env:PYTHON = (Get-Command python.exe).Source
$env:GIT = (Get-Command git.exe).Source
$winapi = "$env:SRC_DIR\Modules\_winapi.c"
$source = Get-Content -Raw -Encoding utf8 $winapi
if ([regex]::Matches($source, "int previousTpSize = 0;").Count -ne 1) {
    throw "Pinned CPython junction privilege buffer source changed"
}
$source = $source.Replace("int previousTpSize = 0;", "DWORD previousTpSize = 0;")
[IO.File]::WriteAllText($winapi, $source, [Text.UTF8Encoding]::new($false))
$semaphore = "$env:SRC_DIR\Modules\_multiprocessing\semaphore.c"
$source = Get-Content -Raw -Encoding utf8 $semaphore
if ([regex]::Matches($source, "(?m)^[ \t]*int sval;").Count -ne 3) {
    throw "Pinned CPython semaphore value source changed"
}
$source = [regex]::Replace($source, "(?m)^([ \t]*)int sval;",
    '#ifdef MS_WINDOWS' + "`n" + '${1}long sval;' + "`n#else`n" +
    '${1}int sval;' + "`n#endif")
[IO.File]::WriteAllText($semaphore, $source, [Text.UTF8Encoding]::new($false))
New-Item -ItemType Directory -Force -Path "$env:SRC_DIR\out" | Out-Null
$response = "$env:SRC_DIR\out\python-build.rsp"
"/p:PlatformToolset=ClangCL`n/p:PreferredToolArchitecture=arm64`n/p:KillPython=false" |
    Set-Content -Encoding utf8 $response
# All upstream external inputs are prepopulated by checksum-pinned sources.
& "$env:SRC_DIR\PCbuild\build.bat" -p ARM64 -c Release -M --no-tkinter `
    "@$response" 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "CPython 3.13.18 ARM64 source build failed" }
& "$env:SRC_DIR\PCbuild\arm64\python.exe" "$env:SRC_DIR\PC\layout" --source $env:SRC_DIR `
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
& "$env:PREFIX\python.exe" -c `
    "from multiprocessing import Semaphore; s = Semaphore(2); assert s._semlock._get_value() == 2 and not s._semlock._is_zero(); assert s.acquire(False) and s.acquire(False); assert s._semlock._get_value() == 0 and s._semlock._is_zero(); s.release(); s.release(); assert s._semlock._get_value() == 2"
if ($LASTEXITCODE) { throw "Native CPython semaphore value regression failed" }
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
