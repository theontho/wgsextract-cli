"""Options that a command's default --help leaves out; --help-all shows them.

An option is advanced when most runs never need it: tuning thresholds, files
that are found automatically, and switches for unusual data or setups. The
options stay accepted; only the default help is shorter. Each option is named
by any of its flags.
"""

# Advanced on every command that has them.
COMMON_ADVANCED = frozenset(
    {
        "--qc-min-depth",
        "--confidence-depth",
        "--confidence-gq",
        "--ploidy-file",
        "--ploidy",
        "--ref-vcf-tab",
        "--no-preflight",
    }
)

_ALIGNERS = ("--preset", "--long-read", "--gpu")
_VEP = ("--vep-cache-version", "--vcf-type", "--add-chr", "--vep-args")

ADVANCED_OPTIONS: dict[str, tuple[str, ...]] = {
    "microarray": (
        "--indel-manifest",
        "--strand-ambiguous",
        "--resolve-rsids",
        "--quirks",
        "--min-gq",
        "--min-dp",
        "--assume-hom-ref",
        "--exclude-filtered",
        "--call-reasons",
        "--aadr-sample-id",
        "--aadr-sex",
        "--parallel",
    ),
    "realign": (*_ALIGNERS, "--input-ref", "--keep-fastq", "--force-extract"),
    "align": _ALIGNERS,
    "lineage y-haplogroup": (
        "--pos-file",
        "--min-reads",
        "--base-quality",
        "--base-majority",
        "--min-score",
        "--no-ref-inference",
    ),
    "lineage y-str": ("--min-reads", "--min-fraction", "--min-mapq"),
    "vep": _VEP,
    "vep run": _VEP,
    "vcf deepvariant": ("--checkpoint", "--batch-size", "--model-type"),
    "benchmark": (
        "--seed",
        "--target-count",
        "--dataset-zip",
        "--dataset-url",
        "--dataset-sha256",
        "--dataset-cache-dir",
    ),
    "example-genome download": ("--aspera-key", "--target-root"),
    "report-issue": ("--log-lines", "--max-url-length"),
}
