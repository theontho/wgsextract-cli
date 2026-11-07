# Data source: reference library readiness values from `wgsextract ref status`.
# Usage: library-state.ps1 [REF_PATH] [GENOME_LIBRARY] [ANNOTATION_VCF] [INPUT_VCF]
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
$statusArgs = @("ref", "status", "--values")
$options = @("--ref", "--genome-library", "--annotation-vcf", "--input")
for ($index = 0; $index -lt $options.Count -and $index -lt $args.Count; $index += 1) {
    if ($args[$index]) {
        $statusArgs += @($options[$index], [string]$args[$index])
    }
}

& (Join-Path $scriptDir "run-wgsextract.ps1") @statusArgs
exit $LASTEXITCODE
