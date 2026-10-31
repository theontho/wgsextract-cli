$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\samtools"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$repo\scratch\vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_MANIFEST_DIR=$repo\packaging\windows-arm64\recipes\htslib" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_SAMTOOLS_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "samtools ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "samtools ARM64 compile failed" }
foreach ($test in @("cram_stop", "bam_seq", "cram_buffers")) {
    & "$build\hts-build\Release\${test}_test.exe"
    if ($LASTEXITCODE) { throw "Native HTS regression failed: $test" }
}
& "$build\hts-build\Release\msvc_files_test.exe"
if ($LASTEXITCODE) { throw "Native file-descriptor regression failed" }
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "samtools ARM64 install failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
$notices = Join-Path $env:PREFIX "Library\share\samtools\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
Copy-Item "$env:SRC_DIR\htslib-1.24\LICENSE" "$notices\htslib.txt"
foreach ($dependency in @("zlib", "libdeflate", "bzip2", "liblzma", "tre", "pthreads", "getopt-win32")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
