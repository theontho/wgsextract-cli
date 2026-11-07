#!/bin/sh
# Data source: reference library readiness values from `wgsextract ref status`.
# Usage: library-state.sh [REF_PATH] [GENOME_LIBRARY] [ANNOTATION_VCF] [INPUT_VCF]
set -eu

script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"
ref_path="${1:-}"
genome_library="${2:-}"
annotation_vcf="${3:-}"
input_vcf="${4:-}"

set -- ref status --values
if [ -n "$ref_path" ]; then
  set -- "$@" --ref "$ref_path"
fi
if [ -n "$genome_library" ]; then
  set -- "$@" --genome-library "$genome_library"
fi
if [ -n "$annotation_vcf" ]; then
  set -- "$@" --annotation-vcf "$annotation_vcf"
fi
if [ -n "$input_vcf" ]; then
  set -- "$@" --input "$input_vcf"
fi

exec sh "$script_dir/run-wgsextract.sh" "$@"
