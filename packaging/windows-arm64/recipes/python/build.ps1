$ErrorActionPreference = "Stop"
$evidence = Join-Path $env:RECIPE_DIR "..\..\..\..\out\windows-arm64\python"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null

if ($env:CONDA_BUILD_TARGET -and $env:CONDA_BUILD_TARGET -ne "win-arm64") {
    throw "This recipe is only for win-arm64"
}
New-Item -ItemType Directory -Force -Path $env:PREFIX | Out-Null
# Rattler treats the NuGet URL as a file source, even with a .zip file_name.
Expand-Archive -LiteralPath "pythonarm64.zip" -DestinationPath "out\nuget"
Copy-Item -Path out\nuget\tools\* -Destination $env:PREFIX -Recurse -Force

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
$native = Start-Process -FilePath "$env:PREFIX\python.exe" `
    -ArgumentList @("-c", '"import time; time.sleep(60)"') -PassThru
try {
    & $audit -Prefix $env:PREFIX -ProcessIds @($native.Id) -Report "$evidence\pe.json"
} finally {
    if (-not $native.HasExited) { Stop-Process -Id $native.Id }
}

& "$env:PREFIX\python.exe" -c "import ssl, sqlite3, bz2, lzma, ctypes; print(ssl.OPENSSL_VERSION)"
if ($LASTEXITCODE) { throw "Native Python extension imports failed" }
& "$env:PREFIX\python.exe" -m pip --version
if ($LASTEXITCODE) { throw "Native Python pip failed" }
