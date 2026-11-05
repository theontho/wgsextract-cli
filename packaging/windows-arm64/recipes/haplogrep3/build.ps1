$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\haplogrep3"
$sdk = Join-Path $env:SRC_DIR "sdk"
New-Item -ItemType Directory -Force -Path $evidence, "$env:PREFIX\bin" | Out-Null
& python "$repo\packaging\windows-arm64\prepare_haplogrep.py" `
    --source "$env:SRC_DIR\haplogrep3\haplogrep3.jar" --output "$env:PREFIX\bin\haplogrep3.jar"
if ($LASTEXITCODE) { throw "Native-only Haplogrep 3 archive preparation failed" }
& python "$repo\packaging\windows-arm64\audit_java_archives.py" `
    --root $env:PREFIX --report "$evidence\archives.json"
if ($LASTEXITCODE) { throw "Haplogrep 3 contains foreign native Java resources" }
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $sdk -Report "$evidence\sdk-pe.json"
# Haplogrep 3 prints its version banner when run without a command. Like the
# CLI, give it its own tree-free configuration instead of the one beside the jar.
$probeHome = Join-Path $evidence "version-home"
New-Item -ItemType Directory -Force -Path $probeHome | Out-Null
[IO.File]::WriteAllText((Join-Path $probeHome "haplogrep3.yaml"), "repositories: []`nphylotrees: []`n")
Push-Location $probeHome
& "$sdk\bin\java.exe" -jar "$env:PREFIX\bin\haplogrep3.jar" 2>&1 |
    Tee-Object "$evidence\version.log"
$versionExit = $LASTEXITCODE
Pop-Location
if ($versionExit) { throw "Native Haplogrep 3 version qualification failed" }
& python "$repo\packaging\windows-arm64\tests\haplogrep_smoke.py" `
    --java "$sdk\bin\java.exe" --original "$env:SRC_DIR\haplogrep3\haplogrep3.jar" `
    --restricted "$env:PREFIX\bin\haplogrep3.jar" --tree "$env:SRC_DIR\tree" `
    --output "$evidence\smoke"
if ($LASTEXITCODE) { throw "Native Haplogrep 3 VCF classification qualification failed" }
$probe = Join-Path $evidence "HaplogrepProbe.java"
@'
class HaplogrepProbe {
    public static void main(String[] args) throws Exception {
        if (!System.getProperty("os.arch").equals("aarch64"))
            throw new IllegalStateException("Native AArch64 required");
        Class.forName("genepi.haplogrep3.App");
        System.in.read();
    }
}
'@ | Set-Content -Encoding utf8 $probe
& "$sdk\bin\javac.exe" -d $evidence $probe
if ($LASTEXITCODE) { throw "Native Haplogrep 3 runtime probe compilation failed" }
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable "$sdk\bin\java.exe" `
    -Arguments @("-cp", "$evidence;$env:PREFIX\bin\haplogrep3.jar", "HaplogrepProbe") `
    -Prefix $sdk -Report "$evidence\runtime-process.json"
