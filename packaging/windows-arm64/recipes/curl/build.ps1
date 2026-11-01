$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$build = Join-Path $env:SRC_DIR "out\arm64-build"
$evidence = Join-Path $repo "out\windows-arm64\curl"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& cmake -S $env:SRC_DIR -B $build -G "Visual Studio 18 2026" -A ARM64 -T ClangCL `
    "-DCMAKE_TOOLCHAIN_FILE=$repo\scratch\vcpkg\scripts\buildsystems\vcpkg.cmake" `
    "-DVCPKG_MANIFEST_DIR=$env:RECIPE_DIR" "-DVCPKG_TARGET_TRIPLET=arm64-windows-static" `
    -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON -DBUILD_STATIC_CURL=ON `
    -DCURL_STATIC_CRT=ON -DCURL_USE_SCHANNEL=ON -DCURL_USE_OPENSSL=OFF `
    -DCURL_ZLIB=ON -DCURL_BROTLI=OFF -DCURL_ZSTD=OFF -DCURL_USE_LIBPSL=OFF `
    -DCURL_USE_LIBSSH2=OFF -DBUILD_TESTING=OFF `
    "-DCMAKE_INSTALL_PREFIX=$env:PREFIX\Library"
if ($LASTEXITCODE) { throw "Native curl configuration failed" }
& cmake --build $build --config Release --parallel 4 2>&1 |
    Tee-Object -FilePath "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "Native curl compilation failed" }
& cmake --install $build --config Release
if ($LASTEXITCODE) { throw "Native curl installation failed" }
$curl = "$env:PREFIX\Library\bin\curl.exe"
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\pe.json"
& $curl --version | Set-Content "$evidence\version.txt"
if ($LASTEXITCODE) { throw "Native curl version qualification failed" }
& python "$repo\packaging\windows-arm64\tests\curl_smoke.py" `
    --curl $curl --output "$evidence\smoke"
if ($LASTEXITCODE) { throw "Native curl redirect/decompression qualification failed" }
$upload = Join-Path $evidence "native-upload.txt"
$uploadUri = ([Uri]$upload).AbsoluteUri
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable $curl `
    -Arguments @("-q", "--fail-early", "--silent", "--show-error", "--fail", "--head", "--max-time", "30",
        "https://curl.se/", "--next", "--silent", "--show-error", "--upload-file", "-", $uploadUri) `
    -InputPrefix "ARM236-native-transfer" -Prefix $env:PREFIX -Report "$evidence\process.json"
if ((Get-Content -Raw $upload) -ne "ARM236-native-transfer") {
    throw "Native curl stdin upload did not preserve bytes"
}
$notices = Join-Path $env:PREFIX "Library\share\curl\licenses"
New-Item -ItemType Directory -Force -Path $notices | Out-Null
Copy-Item "$build\vcpkg_installed\arm64-windows-static\share\zlib\copyright" "$notices\zlib.txt"
