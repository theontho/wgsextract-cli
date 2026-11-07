"""'Next:' hints printed after a command succeeds on a terminal.

Placeholders, filled from the finished run (a hint whose placeholder can't be
filled is skipped):
  {source}  the input as given: --genome ID or --input PATH
  {bam}     the first BAM/CRAM the run wrote
  {vcf}     the first VCF the run wrote

tests/test_cli_help.py checks that every hint parses to a command.

Only suggest what suits any input: a Y haplogroup needs a male sample, so it
isn't suggested before the run has shown there is a Y chromosome.
"""

_TO_CHIP = "wgsextract microarray {source} --formats 23andme_v5"
_MT = "wgsextract lineage mt-haplogroup {source}"
_CHECK_BAM = "wgsextract info --input {bam}"
_REPORT = "wgsextract report {source}"
_ANNOTATE = (
    "wgsextract vcf clinvar --vcf-input {vcf}",
    "wgsextract vep run --input {vcf}",
)

NEXT_STEPS: dict[str, tuple[str, ...]] = {
    "info": (_TO_CHIP, _MT),
    "info coverage-sample": (_TO_CHIP,),
    "info calculate-coverage": (_TO_CHIP,),
    "microarray": (_MT,),
    "lineage y-haplogroup": (_MT,),
    "lineage mt-haplogroup": (_TO_CHIP, _REPORT),
    "basecall": ("wgsextract align --r1 {bam}",),
    "align": (_CHECK_BAM,),
    "realign": (_CHECK_BAM,),
    "vcf snp": _ANNOTATE,
    "vcf indel": _ANNOTATE,
    "vcf clinvar": (_REPORT,),
    "vcf pharmcat": (_REPORT,),
}
