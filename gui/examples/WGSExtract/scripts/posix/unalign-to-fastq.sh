#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  printf 'Usage: %s INPUT_BAM_OR_CRAM OUTPUT_DIR\n' "$0" >&2
  exit 64
fi

input_path="$1"
out_dir="$2"
script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"

if [ -z "$out_dir" ]; then
  out_dir="$(dirname "$input_path")"
fi

mkdir -p "$out_dir"

base_name="$(basename "$input_path")"
case "$base_name" in
  *.bam) base_name="${base_name%.bam}" ;;
  *.cram) base_name="${base_name%.cram}" ;;
  *) base_name="${base_name%.*}" ;;
esac

exec sh "$script_dir/run-wgsextract.sh" \
  bam unalign \
  --input "$input_path" \
  --outdir "$out_dir" \
  --r1 "${base_name}_R1.fastq.gz" \
  --r2 "${base_name}_R2.fastq.gz"
