$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\fastp"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& python "$repo\packaging\windows-arm64\prepare_fastp.py" $env:SRC_DIR
if ($LASTEXITCODE) { throw "Native fastp LLP64 preparation failed" }
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$repo\scratch\vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    "-DWGSE_FASTP_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "fastp ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "fastp ARM64 compile failed" }
& "$build\Release\fastp_cmdline_test.exe"
if ($LASTEXITCODE) { throw "Native fastp argument/error parsing regression failed" }
Get-Content "$evidence\compiler.log" | Select-String '(hwy.*neon-inl\.h|arm_neon\.h)' |
    Set-Content "$evidence\neon-selection.txt"
if (-not (Get-Item "$evidence\neon-selection.txt").Length) {
    throw "Highway NEON selection was not proved"
}
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "fastp ARM64 install failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
& python "$repo\packaging\windows-arm64\tests\fastp_smoke.py" `
    --fastp "$env:PREFIX\Library\bin\fastp.exe" --output "$evidence\smoke"
if ($LASTEXITCODE) { throw "Native fastp gzip/counter qualification failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" `
    -Executable "$env:PREFIX\Library\bin\fastp.exe" `
    -Arguments @("--stdin", "--stdout", "--disable_adapter_trimming", "--dont_eval_duplication",
        "--json", "$evidence\stdin.json", "--html", "$evidence\stdin.html") `
    -InputPrefix ("@arm236`n" + ("ACGT" * 37) + "AC`n+`n" + ("I" * 150) + "`n") `
    -Prefix $env:PREFIX -Report "$evidence\process.json"
$notices = Join-Path $env:PREFIX "Library\share\fastp\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
Copy-Item "$env:SRC_DIR\isa-l\LICENSE" "$notices\isa-l.txt"
foreach ($dependency in @("highway", "libdeflate", "pthreads")) {
    Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\$dependency\copyright" `
        (Join-Path $notices "$dependency.txt")
}
