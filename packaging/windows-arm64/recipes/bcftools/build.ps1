$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\bcftools"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$probe = Join-Path $build "group-probe"
New-Item -ItemType Directory -Force -Path $probe | Out-Null
foreach ($version in @("original", "candidate")) {
    $source = if ($version -eq "original") { "$env:SRC_DIR\original-source\mpileup.c" } else { "$env:SRC_DIR\mpileup.c" }
    $body = [regex]::Match((Get-Content -Raw -Encoding utf8 $source), "(?ms)^static void group_smpl\(.*?^}")
    if (-not $body.Success) { throw "Actual $version grouping function was not found" }
    [IO.File]::WriteAllText("$probe\group_$version.inc", $body.Value + "`n", [Text.UTF8Encoding]::new($false))
}
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$repo\scratch\vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_MANIFEST_DIR=$repo\packaging\windows-arm64\recipes\htslib" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_BCFTOOLS_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "bcftools ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "bcftools ARM64 compile failed" }
foreach ($test in @("cram_stop", "bam_seq", "cram_buffers")) {
    & "$build\hts-build\Release\${test}_test.exe"
    if ($LASTEXITCODE) { throw "Native HTS regression failed: $test" }
}
& "$build\hts-build\Release\msvc_files_test.exe"
if ($LASTEXITCODE) { throw "Native file-descriptor regression failed" }
& "$build\Release\group_copy_test.exe"
if ($LASTEXITCODE) { throw "Native actual-function grouping regression failed" }
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "bcftools ARM64 install failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
$notices = Join-Path $env:PREFIX "Library\share\bcftools\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
Copy-Item "$env:SRC_DIR\htslib-1.24\LICENSE" "$notices\htslib.txt"
foreach ($dependency in @("zlib", "libdeflate", "bzip2", "liblzma", "tre", "pthreads", "getopt-win32")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
