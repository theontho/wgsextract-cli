$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\haplocheck"
$sdk = Join-Path $env:SRC_DIR "sdk"
New-Item -ItemType Directory -Force -Path $evidence, "$env:PREFIX\bin" | Out-Null
# mutserve, haplocheck's variant caller, must sit beside haplocheck.jar.
foreach ($jar in "haplocheck.jar", "mutserve.jar") {
    & python "$repo\packaging\windows-arm64\prepare_haplogrep.py" `
        --source "$env:SRC_DIR\haplocheck\$jar" --output "$env:PREFIX\bin\$jar"
    if ($LASTEXITCODE) { throw "Native-only $jar archive preparation failed" }
    & "$sdk\bin\java.exe" -jar "$env:PREFIX\bin\$jar" --version 2>&1 |
        Tee-Object "$evidence\$jar.version.log"
    if ($LASTEXITCODE) { throw "Native $jar version qualification failed" }
}
& python "$repo\packaging\windows-arm64\audit_java_archives.py" `
    --root $env:PREFIX --report "$evidence\archives.json"
if ($LASTEXITCODE) { throw "haplocheck contains foreign native Java resources" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $sdk -Report "$evidence\sdk-pe.json"
& python "$repo\packaging\windows-arm64\tests\haplogrep_smoke.py" `
    --java "$sdk\bin\java.exe" --original "$env:SRC_DIR\haplocheck\haplocheck.jar" `
    --restricted "$env:PREFIX\bin\haplocheck.jar" --output "$evidence\smoke"
if ($LASTEXITCODE) { throw "Native haplocheck VCF contamination qualification failed" }
$probe = Join-Path $evidence "HaplocheckProbe.java"
@'
class HaplocheckProbe {
    public static void main(String[] args) throws Exception {
        if (!System.getProperty("os.arch").equals("aarch64"))
            throw new IllegalStateException("Native AArch64 required");
        Class.forName("genepi.haplocheck.App");
        Class.forName("genepi.mut.App");
        System.in.read();
    }
}
'@ | Set-Content -Encoding utf8 $probe
& "$sdk\bin\javac.exe" -d $evidence $probe
if ($LASTEXITCODE) { throw "Native haplocheck runtime probe compilation failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable "$sdk\bin\java.exe" `
    -Arguments @("-cp", "$evidence;$env:PREFIX\bin\haplocheck.jar;$env:PREFIX\bin\mutserve.jar", "HaplocheckProbe") `
    -Prefix $sdk -Report "$evidence\runtime-process.json"
