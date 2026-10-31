$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\haplogrep"
$sdk = Join-Path $env:SRC_DIR "sdk"
New-Item -ItemType Directory -Force -Path $evidence, "$env:PREFIX\bin" | Out-Null
Copy-Item "$env:SRC_DIR\haplogrep\haplogrep.jar" "$env:PREFIX\bin\haplogrep.jar"
& python "$repo\packaging\windows-arm64\audit_java_archives.py" `
    --root $env:PREFIX --report "$evidence\archives.json"
if ($LASTEXITCODE) { throw "Haplogrep contains foreign native Java resources" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $sdk -Report "$evidence\sdk-pe.json"
& "$sdk\bin\java.exe" -jar "$env:PREFIX\bin\haplogrep.jar" --version 2>&1 |
    Tee-Object "$evidence\version.log"
if ($LASTEXITCODE) { throw "Native Haplogrep version qualification failed" }
$probe = Join-Path $evidence "HaplogrepProbe.java"
@'
class HaplogrepProbe {
    public static void main(String[] args) throws Exception {
        if (!System.getProperty("os.arch").equals("aarch64"))
            throw new IllegalStateException("Native AArch64 required");
        Class.forName("genepi.App");
        System.in.read();
    }
}
'@ | Set-Content -Encoding utf8 $probe
& "$sdk\bin\javac.exe" -d $evidence $probe
if ($LASTEXITCODE) { throw "Native Haplogrep runtime probe compilation failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable "$sdk\bin\java.exe" `
    -Arguments @("-cp", "$evidence;$env:PREFIX\bin\haplogrep.jar", "HaplogrepProbe") `
    -Prefix $sdk -Report "$evidence\runtime-process.json"
