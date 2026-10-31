#!/bin/sh
# Run a real WGS Extract workload against the HG00096 mini genome.
#
# Usage: workload.sh <wgsextract-launcher> <dataset-dir> <out-dir>
#
# Every step leaves <out-dir>/evidence/<step>.{rc,seconds,log}. This script never
# decides whether the run passed: the host's oracle reads the evidence and the
# output files, so a step that "succeeds" without producing correct output fails.
set -u

WGSE="$1"
DATA="$2"
OUT="$3"
EVIDENCE="$OUT/evidence"
mkdir -p "$EVIDENCE"

REF="$DATA/hg19-mini.fa.gz"
BAM="$DATA/HG00096.hg19-mini.bam"
CRAM="$DATA/HG00096.hg19-mini.cram"
VCF="$DATA/HG00096.hg19-mini.vcf.gz"
TARGETS="$DATA/HG00096.hg19-mini.targets.tab.gz"
R1="$DATA/HG00096.hg19-mini_R1.fastq.gz"
R2="$DATA/HG00096.hg19-mini_R2.fastq.gz"

step() {
    name="$1"
    shift
    start=$(date +%s)
    "$@" > "$EVIDENCE/$name.log" 2>&1
    rc=$?
    echo "$rc" > "$EVIDENCE/$name.rc"
    echo $(( $(date +%s) - start )) > "$EVIDENCE/$name.seconds"
    echo "step $name rc=$rc"
}

step help        "$WGSE" --help
step deps        "$WGSE" deps check
# The reference library bootstrap is a real download (GitHub release asset with a
# verified digest); microarray needs its vendor templates.
step bootstrap   "$WGSE" ref bootstrap --ref "$OUT/reference"
step info        "$WGSE" info --input "$BAM" --ref "$REF" --outdir "$OUT/info" --detailed
step info_cram   "$WGSE" info --input "$CRAM" --ref "$REF" --outdir "$OUT/info_cram"
step mt_bam      "$WGSE" extract mt-bam --input "$BAM" --ref "$REF" --outdir "$OUT/mt_bam"
step mito_vcf    "$WGSE" extract mito-vcf --input "$BAM" --ref "$REF" --outdir "$OUT/mito_vcf"
step mt_haplo    "$WGSE" lineage mt-haplogroup --input "$BAM" --ref "$REF" --outdir "$OUT/mt_haplo"
step y_haplo     "$WGSE" lineage y-haplogroup --input "$BAM" --ref "$REF" --outdir "$OUT/y_haplo"
step snp         "$WGSE" vcf snp --input "$BAM" --ref "$REF" --region 20 --ploidy 2 --outdir "$OUT/snp"
step microarray  "$WGSE" microarray --input "$BAM" --ref "$REF" --ref-vcf-tab "$TARGETS" --formats 23andme_v5 --outdir "$OUT/microarray"
step align       "$WGSE" align --r1 "$R1" --r2 "$R2" --ref "$REF" --outdir "$OUT/align"
# The scenario explicitly installed this optional environment before the workload.
step gatk        "$WGSE" vcf gatk --input "$BAM" --ref "$REF" --region 20:1-200000 --outdir "$OUT/gatk"
# A bad input must fail loudly, not "succeed" with no output.
step must_fail   "$WGSE" info --input "$DATA/does-not-exist.bam" --outdir "$OUT/must_fail"
echo "workload finished"
