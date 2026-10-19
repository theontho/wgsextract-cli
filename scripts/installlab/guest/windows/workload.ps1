# Run the real WGS Extract workload (the same steps as guest/workload.sh) the
# way a Windows user does after install_windows.bat: `pixi run wgsextract ...`
# from the install folder.
#
# Usage: workload.ps1 <install-dir> <dataset-dir> <out-dir>
param([string]$InstallDir, [string]$Data, [string]$Out)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lab-common.ps1")
$Evidence = Join-Path $Out "evidence"
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null

$REF = Join-Path $Data "hg19-mini.fa.gz"
$BAM = Join-Path $Data "HG00096.hg19-mini.bam"
$CRAM = Join-Path $Data "HG00096.hg19-mini.cram"
$TARGETS = Join-Path $Data "HG00096.hg19-mini.targets.tab.gz"
$R1 = Join-Path $Data "HG00096.hg19-mini_R1.fastq.gz"
$R2 = Join-Path $Data "HG00096.hg19-mini_R2.fastq.gz"

function W([string]$Name, [string[]]$CliArgs) {
    Invoke-Step -Evidence $Evidence -Name $Name -WorkingDirectory $InstallDir `
        -Command (@("pixi", "run", "wgsextract") + $CliArgs) | Out-Null
}

W "help" @("--help")
W "deps" @("deps", "check")
W "bootstrap" @("ref", "bootstrap", "--ref", (Join-Path $Out "reference"))
W "info" @("info", "--input", $BAM, "--ref", $REF, "--outdir", (Join-Path $Out "info"), "--detailed")
W "info_cram" @("info", "--input", $CRAM, "--ref", $REF, "--outdir", (Join-Path $Out "info_cram"))
W "mt_bam" @("extract", "mt-bam", "--input", $BAM, "--ref", $REF, "--outdir", (Join-Path $Out "mt_bam"))
W "mito_vcf" @("extract", "mito-vcf", "--input", $BAM, "--ref", $REF, "--outdir", (Join-Path $Out "mito_vcf"))
W "mt_haplo" @("lineage", "mt-haplogroup", "--input", $BAM, "--ref", $REF, "--outdir", (Join-Path $Out "mt_haplo"))
W "y_haplo" @("lineage", "y-haplogroup", "--input", $BAM, "--ref", $REF, "--outdir", (Join-Path $Out "y_haplo"))
W "snp" @("vcf", "snp", "--input", $BAM, "--ref", $REF, "--region", "20", "--ploidy", "2", "--outdir", (Join-Path $Out "snp"))
W "microarray" @("microarray", "--input", $BAM, "--ref", $REF, "--ref-vcf-tab", $TARGETS, "--formats", "23andme_v5", "--outdir", (Join-Path $Out "microarray"))
W "align" @("align", "--r1", $R1, "--r2", $R2, "--ref", $REF, "--outdir", (Join-Path $Out "align"))
W "gatk" @("vcf", "gatk", "--input", $BAM, "--ref", $REF, "--region", "20:1-200000", "--outdir", (Join-Path $Out "gatk"))
W "must_fail" @("info", "--input", (Join-Path $Data "does-not-exist.bam"), "--outdir", (Join-Path $Out "must_fail"))
Write-Host "workload finished"
