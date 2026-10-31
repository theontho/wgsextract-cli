$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $env:RECIPE_DIR "..\..\..\..")).Path
$evidence = Join-Path $repo "out\windows-arm64\fastqc"
$source = Join-Path $env:SRC_DIR "fastqc"
$sdk = Join-Path $env:SRC_DIR "sdk"
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $sdk -Report "$evidence\sdk-pe.json"
& python "$repo\packaging\windows-arm64\prepare_fastqc.py" `
    --source $source --prefix $env:PREFIX --repo $repo
if ($LASTEXITCODE) { throw "Native-only FastQC distribution preparation failed" }
$distribution = Join-Path $env:PREFIX "Library\share\fastqc"
$classes = foreach ($directory in @("uk", "net", "org")) {
    Get-ChildItem -Path (Join-Path $source $directory) -Filter *.java -Recurse |
        ForEach-Object { '"' + $_.FullName.Replace('\', '/') + '"' }
}
$response = Join-Path $evidence "javac-sources.rsp"
$classes | Set-Content -Encoding utf8NoBOM $response
& "$sdk\bin\javac.exe" --release 17 -encoding UTF-8 -cp "$source\*" `
    -d $distribution "@$response" 2>&1 | Tee-Object "$evidence\compiler.log"
if ($LASTEXITCODE) { throw "Native-JDK FastQC compilation failed" }
& python "$repo\packaging\windows-arm64\audit_java_archives.py" `
    --root $distribution --report "$evidence\archives.json"
if ($LASTEXITCODE) { throw "FastQC contains foreign native Java resources" }
& python "$env:PREFIX\bin\fastqc" --java "$sdk\bin\java.exe" --version 2>&1 |
    Tee-Object "$evidence\version.log"
if ($LASTEXITCODE) { throw "Native FastQC version qualification failed" }
& python "$repo\packaging\windows-arm64\tests\fastqc_smoke.py" `
    --launcher "$env:PREFIX\bin\fastqc" --java "$sdk\bin\java.exe" --output "$evidence\smoke"
if ($LASTEXITCODE) { throw "Native FastQC FASTQ/FAST5-boundary qualification failed" }
$live = Join-Path $evidence "live"
New-Item -ItemType Directory -Force -Path $live | Out-Null
& "$repo\scripts\qualify_windows_arm64_process.ps1" -Executable "$sdk\bin\java.exe" `
    -Arguments @("-Xmx256m", "-Djava.awt.headless=true", "-Dfastqc.output_dir=$live",
        "-cp", "$distribution;$distribution\*", "uk.ac.babraham.FastQC.FastQCApplication",
        "stdin:arm236.fastq") `
    -InputPrefix ("@arm236`n" + ("ACGT" * 37) + "AC`n+`n" + ("I" * 150) + "`n") `
    -Prefix $sdk -Report "$evidence\process.json"
& "$repo\scripts\audit_windows_arm64.ps1" -Prefix $env:PREFIX -Report "$evidence\package-pe.json"
