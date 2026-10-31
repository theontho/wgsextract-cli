$ErrorActionPreference = "Stop"

if ($env:CONDA_BUILD_TARGET -and $env:CONDA_BUILD_TARGET -ne "win-arm64") {
    throw "This recipe is only for win-arm64"
}
New-Item -ItemType Directory -Force -Path $env:PREFIX | Out-Null
Copy-Item -Path tools\* -Destination $env:PREFIX -Recurse -Force

# Upstream NuGet contains unused x86/x64 distlib launcher templates and an x64
# C++ exception runtime. Native Python and its extensions must load without it.
$remove = @(
    "vcruntime140_1.dll",
    "Lib\site-packages\pip\_vendor\distlib\t32.exe",
    "Lib\site-packages\pip\_vendor\distlib\t64.exe",
    "Lib\site-packages\pip\_vendor\distlib\w32.exe",
    "Lib\site-packages\pip\_vendor\distlib\w64.exe"
)
foreach ($relative in $remove) {
    Remove-Item -LiteralPath (Join-Path $env:PREFIX $relative) -ErrorAction Stop
}
$audit = Join-Path $env:RECIPE_DIR "..\..\..\..\scripts\audit_windows_arm64.ps1"
& $audit -Prefix $env:PREFIX -Report "out/python-arm64-pe.json"

& "$env:PREFIX\python.exe" -c "import ssl, sqlite3, bz2, lzma, ctypes; print(ssl.OPENSSL_VERSION)"
if ($LASTEXITCODE) { throw "Native Python extension imports failed" }
& "$env:PREFIX\python.exe" -m pip --version
if ($LASTEXITCODE) { throw "Native Python pip failed" }
