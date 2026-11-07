$ErrorActionPreference = "Stop"

if ($args.Count -lt 1) {
    [Console]::Error.WriteLine("Usage: unalign-to-fastq.ps1 INPUT_BAM_OR_CRAM [OUTPUT_DIR] [ARG...]")
    exit 2
}

$inputPath = $args[0]
$outDir = if ($args.Count -gt 1 -and $args[1]) { $args[1] } else { Split-Path -Parent $inputPath }
$scriptDir = Split-Path -Parent $PSCommandPath
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$baseName = [System.IO.Path]::GetFileNameWithoutExtension($inputPath)
$extraArgs = @($args | Select-Object -Skip 2)

& (Join-Path $scriptDir "run-wgsextract.ps1") bam unalign --input $inputPath --outdir $outDir --r1 "${baseName}_R1.fastq.gz" --r2 "${baseName}_R2.fastq.gz" @extraArgs
exit $LASTEXITCODE
