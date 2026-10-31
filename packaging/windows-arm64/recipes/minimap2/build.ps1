$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$vcpkg = Join-Path $repo "scratch\vcpkg"
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\minimap2"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
if (-not (Test-Path "$vcpkg\scripts\buildsystems\vcpkg.cmake")) {
    throw "The pinned build-only vcpkg checkout is required; see windows_arm64.yml"
}
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_MINIMAP2_SOURCE=$env:SRC_DIR" "-Darm_neon=1" `
    "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "minimap2 ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "minimap2 ARM64 compile failed" }
Get-Content "$evidence\compiler.log" |
    Select-String '(sse2neon[\\/]emmintrin\.h|arm_neon\.h)' |
    Set-Content "$evidence\neon-selection.txt"
if (-not (Get-Item "$evidence\neon-selection.txt").Length) {
    throw "Missing evidence for upstream arm_neon=1 kernels"
}
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "minimap2 ARM64 install failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
& python "$repo\packaging\windows-arm64\tests\minimap2_smoke.py" `
    --minimap2 "$env:PREFIX\Library\bin\minimap2.exe" --output "$evidence\smoke" `
    --expected-sha256 f3075dde712eb8219e2ab446f636f51c5d6ce009c5b10189eb09a26cdee05fa3
if ($LASTEXITCODE) { throw "minimap2 native alignment smoke test failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" `
    -Executable "$env:PREFIX\Library\bin\minimap2.exe" `
    -Arguments @("-a", "-x", "sr", "$evidence\smoke\reference.mmi", "-") `
    -Prefix $env:PREFIX -Report "$evidence\process.json"

$notices = Join-Path $env:PREFIX "Library\share\minimap2\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
foreach ($dependency in @("zlib", "pthreads")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
Copy-Item "$env:SRC_DIR\sse2neon\emmintrin.h" "$notices\sse2neon.h"
