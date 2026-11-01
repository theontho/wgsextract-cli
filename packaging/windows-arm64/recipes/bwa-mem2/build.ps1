$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$vcpkg = Join-Path $repo "scratch\vcpkg"
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\bwa-mem2"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
if (-not (Test-Path "$vcpkg\scripts\buildsystems\vcpkg.cmake")) {
    throw "Pinned build-only vcpkg checkout is required"
}
& git -C $env:SRC_DIR init --quiet
if ($LASTEXITCODE) { throw "Index patch worktree initialization failed" }
& git -C $env:SRC_DIR apply --ignore-space-change "$env:SRC_DIR\index64\0002-index64-chunked-io.patch"
if ($LASTEXITCODE) { throw "Pinned index64 patch failed" }
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_MEM2_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "MEM2 ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "MEM2 ARM64 compile failed" }
Get-Content "$evidence\compiler.log" |
    Select-String '(sse2neon\.h|arm_neon\.h)' |
    Set-Content "$evidence\neon-selection.txt"
if (-not (Get-Item "$evidence\neon-selection.txt").Length) {
    throw "Banded SW sse2neon selection was not proved"
}
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "MEM2 ARM64 install failed" }
$bwa = "$env:PREFIX\Library\bin\bwa-mem2.exe"
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
& "$build\Release\mem2_io_test.exe" "$build\seek.bin" "$build\mini.fa"
if ($LASTEXITCODE) { throw "Native >4-GiB seek/partial/chunk regression failed" }
& $bwa index "$build\mini.fa"
if ($LASTEXITCODE) { throw "MEM2 tiny index build failed" }
foreach ($line in Get-Content "$env:SRC_DIR\index64\tiny-index.sha256") {
    $hash, $file = $line -split '\s+', 2
    if ((Get-FileHash (Join-Path $build $file) -Algorithm SHA256).Hash -ne $hash) {
        throw "Native index differs from Linux: $file"
    }
}
& python "$repo\packaging\windows-arm64\tests\bwa_smoke.py" `
    --bwa $bwa --output "$evidence\smoke" `
    --expected-sha256 de8085bffa55e5578e3b39de5e939a55c917e87334103804e01e9ce0dbac1d72
if ($LASTEXITCODE) { throw "MEM2 native paired-read truth/thread gate failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable $bwa `
    -Arguments @("mem", "$evidence\smoke\reference.fa", "-") `
    -Prefix $env:PREFIX -Report "$evidence\process.json"
$notices = Join-Path $env:PREFIX "Library\share\bwa-mem2\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
foreach ($dependency in @("zlib", "pthreads", "getopt-win32")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
Copy-Item "$env:SRC_DIR\sse2neon\LICENSE" "$notices\sse2neon.txt"
