"""'Next:' hints printed after a command succeeds on a terminal.

Placeholders, filled from the finished run (a hint whose placeholder can't be
filled is skipped):
  {source}  the input as given: --genome ID or --input PATH
  {bam}     the first BAM/CRAM the run wrote
  {vcf}     the first VCF the run wrote

tests/test_cli_help.py checks that every hint parses to a command.
"""

_TO_CHIP = "wgsextract microarray {source} --formats 23andme_v5"
_Y = "wgsextract lineage y-haplogroup {source}"
_MT = "wgsextract lineage mt-haplogroup {source}"
_CHECK_BAM = "wgsextract info --input {bam}"
_ANNOTATE = (
    "wgsextract vcf clinvar --vcf-input {vcf}",
    "wgsextract vep --input {vcf}",
)

NEXT_STEPS: dict[str, tuple[str, ...]] = {
    "info": (_TO_CHIP, _Y),
    "info coverage-sample": (_TO_CHIP,),
    "info calculate-coverage": (_TO_CHIP,),
    "microarray": (_Y, _MT),
    "lineage y-haplogroup": (_MT,),
    "lineage mt-haplogroup": (_Y,),
    "align": (_CHECK_BAM,),
    "realign": (_CHECK_BAM,),
    "vcf snp": _ANNOTATE,
    "vcf indel": _ANNOTATE,
}
