param(
    [Parameter(Mandatory=$true)][string]$Samtools,
    [Parameter(Mandatory=$true)][string]$Output
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$sam = Join-Path $Output input.sam
$bam = Join-Path $Output input.bam
$header = Join-Path $Output header.sam
$rewritten = Join-Path $Output rewritten.bam
@(
    "@HD`tVN:1.6`tSO:coordinate",
    "@SQ`tSN:chrTest`tLN:200",
    "pairA`t99`tchrTest`t11`t60`t20M`t=`t31`t40`tACGTACGTACGTACGTACGT`tIIIIIIIIIIIIIIIIIIII",
    "pairA`t147`tchrTest`t31`t60`t20M`t=`t11`t-40`tACGTACGTACGTACGTACGT`tIIIIIIIIIIIIIIIIIIII"
) | Set-Content -Encoding utf8NoBOM $sam
& $Samtools view -b -o $bam $sam
if ($LASTEXITCODE) { throw "Native reheader input preparation failed" }
$before = @(& $Samtools view $bam)
if ($LASTEXITCODE) { throw "Native reheader input decode failed" }
@(& $Samtools view -H $bam) -replace "SO:coordinate", "SO:unsorted" |
    Set-Content -Encoding utf8NoBOM $header
if ($LASTEXITCODE) { throw "Native reheader header preparation failed" }
& $Samtools reheader $header $bam > $rewritten
if ($LASTEXITCODE) { throw "Native BAM reheader failed or aborted after closing stdout" }
& $Samtools quickcheck $rewritten
if ($LASTEXITCODE) { throw "Native reheader output is incomplete" }
$after = @(& $Samtools view $rewritten)
if ($LASTEXITCODE -or ($before -join "`n") -cne ($after -join "`n")) {
    throw "Native reheader changed alignment records"
}
$afterHeader = @(& $Samtools view -H $rewritten)
if ($LASTEXITCODE -or !($afterHeader -match "SO:unsorted")) {
    throw "Native reheader did not update the sort-order header"
}
@{records=$after.Count; stdout_lifetime_pass=$true; ordered_records_identical=$true} |
    ConvertTo-Json | Set-Content (Join-Path $Output result.json)
