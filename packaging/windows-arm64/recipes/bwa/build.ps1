$ErrorActionPreference = "Stop"
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $env:RECIPE_DIR "..\..\..\..\out\windows-arm64\bwa"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$vcpkg = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..\scratch\vcpkg")).Path
if (-not (Test-Path "$vcpkg\vcpkg.exe")) {
    throw "Install the pinned vcpkg build prerequisite; no runtime auto-install"
}

& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_BWA_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "BWA ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 --verbose 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "BWA ARM64 compile failed" }

# The compiler's /showIncludes proves KSW selected neon_sse.h, not scalar_sse.h.
Get-Content "$evidence\compiler.log" |
    Select-String -Pattern 'neon_sse.h' |
    Set-Content -Path "$evidence\ksw-neon-selection.txt"
if (-not (Get-Item "$evidence\ksw-neon-selection.txt").Length) {
    throw "KSW NEON selection was not proved"
}
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "BWA ARM64 install failed" }
$audit = Join-Path $env:RECIPE_DIR "..\..\..\..\scripts\audit_windows_arm64.ps1"
& $audit -Prefix $env:PREFIX -Report "$evidence\pe.json"

$probe = "$build\Release\bwa_sa_test.exe"
$bwa = "$build\Release\bwa.exe"
$reference = "$build\sa-test.fa"
& $probe --write-ref $reference
if ($LASTEXITCODE) { throw "BWA suffix-array test input failed" }
& $bwa index $reference
if ($LASTEXITCODE) { throw "BWA native indexing failed" }
& $probe "$reference.bwt" "$reference.sa"
if ($LASTEXITCODE) { throw "BWA native suffix-array equivalence failed" }
$smoke = Join-Path $env:RECIPE_DIR "..\..\tests\bwa_smoke.py"
& python $smoke --bwa $bwa --output "$evidence\smoke" `
    --expected-sha256 "efb0d8004175411365b5f740cd833ebcbaf78f39c6aa1fa72a232d5e92b121b6"
if ($LASTEXITCODE) { throw "BWA native paired-read truth/thread equivalence failed" }
$qualify = Join-Path $env:RECIPE_DIR "..\..\..\..\scripts\qualify_windows_arm64_process.ps1"
& $qualify -Executable "$env:PREFIX\Library\bin\bwa.exe" `
    -Arguments @("mem", "$evidence\smoke\reference.fa", "-") `
    -Prefix $env:PREFIX -Report "$evidence\process.json"

$notices = Join-Path $env:PREFIX "Library\share\bwa\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
foreach ($dependency in @("zlib", "pthreads", "getopt-win32")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
