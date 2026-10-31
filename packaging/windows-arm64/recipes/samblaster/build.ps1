$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\samblaster"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& cmake -S $env:RECIPE_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DWGSE_SAMBLASTER_SOURCE=$env:SRC_DIR" "-DCMAKE_INSTALL_PREFIX=$env:PREFIX"
if ($LASTEXITCODE) { throw "samblaster ARM64 configure failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "samblaster ARM64 compile failed" }
& "$build\Release\msvc_format_test.exe"
if ($LASTEXITCODE) { throw "Native MSVC allocated-format regression failed" }
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "samblaster ARM64 install failed" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
& "$repo\scripts\qualify_windows_arm64_process.ps1" `
    -Executable "$env:PREFIX\Library\bin\samblaster.exe" -Arguments @("--ignoreUnmated") `
    -Prefix $env:PREFIX -Report "$evidence\process.json" `
    -InputPrefix "@HD`tVN:1.6`tSO:queryname`n@SQ`tSN:fixture`tLN:1000`n"
