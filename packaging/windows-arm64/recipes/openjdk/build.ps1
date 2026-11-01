$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\openjdk"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:SRC_DIR `
    -Report "$evidence\upstream-pe.json"
$runtime = Join-Path $env:PREFIX "Library"
& "$env:SRC_DIR\bin\jlink.exe" --add-modules java.se,jdk.crypto.ec,jdk.unsupported,jdk.zipfs,jdk.localedata `
    --strip-debug --no-header-files --no-man-pages --output $runtime
if ($LASTEXITCODE) { throw "Native ARM64 Java runtime image build failed" }
$java = Join-Path $runtime "bin\java.exe"
& $java -XshowSettings:properties -version 2>&1 |
    Tee-Object -FilePath "$evidence\version.log"
if ($LASTEXITCODE) { throw "Native ARM64 Java launch failed" }
if (-not (Select-String -Path "$evidence\version.log" -Pattern 'os.arch = aarch64')) {
    throw "Java did not report native AArch64"
}
$probe = Join-Path $evidence "Arm64Probe.java"
@'
class Arm64Probe {
    public static void main(String[] args) throws Exception {
        if (!System.getProperty("os.arch").equals("aarch64"))
            throw new IllegalStateException("Native AArch64 required");
        System.in.read();
    }
}
'@ | Set-Content -Path $probe -Encoding utf8
& "$env:SRC_DIR\bin\javac.exe" -d $evidence $probe
if ($LASTEXITCODE) { throw "Native Java process probe compile failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable $java `
    -Arguments @("-cp", $evidence, "Arm64Probe") -Prefix $env:PREFIX `
    -Report "$evidence\process.json"
