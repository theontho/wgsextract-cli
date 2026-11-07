# Runs `wgsextract vcf snp|indel`, adding a bcftools ploidy named after the
# reference FASTA's build when none is given. The CLI finds ploidy_<build>.txt
# in a bootstrapped reference library on its own, but a FASTA outside one (such
# as the test genome's) has no ploidy file and the CLI would refuse to call.
$ErrorActionPreference = "Stop"

$runWgsextract = Join-Path (Split-Path -Parent $PSCommandPath) "run-wgsextract.ps1"
$forwardArgs = @($args | ForEach-Object { [string]$_ })

if ($forwardArgs.Count -lt 1 -or $forwardArgs[0] -notin @("snp", "indel")) {
    [Console]::Error.WriteLine("Usage: run-wgsextract-vcf.ps1 snp|indel [ARG...]")
    exit 64
}

$ref = ""
for ($index = 0; $index -lt $forwardArgs.Count; $index += 1) {
    $argument = $forwardArgs[$index]
    if ($argument -in @("--ploidy", "--ploidy-file") -or $argument.StartsWith("--ploidy=") -or $argument.StartsWith("--ploidy-file=")) {
        & $runWgsextract vcf @forwardArgs
        exit $LASTEXITCODE
    }
    if ($argument -eq "--ref" -and $index + 1 -lt $forwardArgs.Count) {
        $ref = $forwardArgs[$index + 1]
    } elseif ($argument.StartsWith("--ref=")) {
        $ref = $argument.Substring("--ref=".Length)
    }
}

$ploidy = ""
if ($ref -and (Test-Path -LiteralPath $ref -PathType Leaf)) {
    $name = [System.IO.Path]::GetFileName($ref).ToLowerInvariant()
    if ($name -match "hg19|hg37|grch37|hs37") {
        $ploidy = "GRCh37"
    } elseif ($name -match "hg38|grch38|hs38") {
        $ploidy = "GRCh38"
    }
}

if ($ploidy) {
    & $runWgsextract vcf @forwardArgs --ploidy $ploidy
} else {
    & $runWgsextract vcf @forwardArgs
}
exit $LASTEXITCODE
