#!/bin/sh
# Runs `wgsextract vcf snp|indel`, adding a bcftools ploidy named after the
# reference FASTA's build when none is given. The CLI finds ploidy_<build>.txt
# in a bootstrapped reference library on its own, but a FASTA outside one (such
# as the test genome's) has no ploidy file and the CLI would refuse to call.
set -eu

script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
  snp|indel) ;;
  *)
    printf 'Usage: %s snp|indel [ARG...]\n' "$0" >&2
    exit 64
    ;;
esac

ref=""
previous=""
for argument in "$@"; do
  case "$argument" in
    --ploidy|--ploidy=*|--ploidy-file|--ploidy-file=*)
      exec sh "$script_dir/run-wgsextract.sh" vcf "$@"
      ;;
    --ref=*) ref="${argument#--ref=}" ;;
    *)
      if [ "$previous" = "--ref" ]; then
        ref="$argument"
      fi
      ;;
  esac
  previous="$argument"
done

ploidy=""
if [ -f "$ref" ]; then
  case "$(basename "$ref" | tr '[:upper:]' '[:lower:]')" in
    *hg19* | *hg37* | *grch37* | *hs37*) ploidy="GRCh37" ;;
    *hg38* | *grch38* | *hs38*) ploidy="GRCh38" ;;
  esac
fi

if [ -n "$ploidy" ]; then
  exec sh "$script_dir/run-wgsextract.sh" vcf "$@" --ploidy "$ploidy"
fi
exec sh "$script_dir/run-wgsextract.sh" vcf "$@"
