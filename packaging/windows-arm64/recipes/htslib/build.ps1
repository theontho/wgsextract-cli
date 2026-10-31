$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$vcpkg = Join-Path $repo "scratch\vcpkg"
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\htslib"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_HTSLIB_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "HTS ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "HTS ARM64 compile failed" }
Get-Content "$evidence\compiler.log" | Select-String 'arm_neon\.h' |
    Set-Content "$evidence\neon-selection.txt"
if (-not (Get-Item "$evidence\neon-selection.txt").Length) {
    throw "Native CRAM NEON compilation was not proved"
}
foreach ($test in @("cram_stop", "bam_seq", "cram_buffers")) {
    & "$build\Release\${test}_test.exe"
    if ($LASTEXITCODE) { throw "Native HTS regression failed: $test" }
}
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "HTS ARM64 install failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
$notices = Join-Path $env:PREFIX "Library\share\htslib\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
foreach ($dependency in @("zlib", "libdeflate", "bzip2", "liblzma", "tre", "pthreads", "getopt-win32")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
