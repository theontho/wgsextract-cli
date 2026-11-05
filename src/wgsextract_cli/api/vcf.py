"""``vcf``: call variants, and annotate, filter, tabulate and analyse VCFs.

``snp`` and ``indel`` call small variants from a BAM/CRAM with bcftools. The
other operations read a VCF: ``vcf_input``, else ``input``. Their outputs go
to ``outdir``, else beside that VCF. Annotators take their data file from the
field, else config.toml, else the reference library (``ref``); see
``wgsextract ref`` to download it."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.table_writer import FORMATS, table_format_type
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    depth_param,
    input_param,
    optional_input_param,
    outdir_param,
    ref_param,
    threads_param,
)


@dataclass(frozen=True, kw_only=True)
class _SmallVariantParams(Params):
    input: Path = input_param("BAM or CRAM to call.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    ploidy_file: Path | None = param(
        None,
        help="File defining ploidy per chromosome (auto-resolved from --ref if possible)",
        path="file",
        exclusive="ploidy",
    )
    ploidy: str | None = param(
        None, help="Predefined ploidy name or value", exclusive="ploidy"
    )
    region: str | None = param(
        None,
        help="Chromosomal region (e.g. chrM, chrY:10000-20000)",
        flags=("-r", "--region"),
    )
    parallel: bool = param(
        False,
        help="Call contigs in parallel with up to --threads workers. SNP calling "
        "then recalls the stretches with sites deeper than 255 reads in one "
        "stream, because bcftools subsamples them with one random stream per "
        "process, so the calls match the default single stream.",
        action="store_true",
    )
    qc_min_depth: float | None = depth_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.ploidy is not None and self.ploidy_file is not None:
            raise UsageError("Use either --ploidy or --ploidy-file, not both.")


@dataclass(frozen=True, kw_only=True)
class VcfSnpParams(_SmallVariantParams):
    """Call SNPs from a BAM/CRAM into ``snps.vcf.gz``.

    Ploidy comes from ``ploidy`` or ``ploidy_file``, else the reference
    library's ploidy file, else bcftools' built-in GRCh37/GRCh38 table.
    """

    COMMAND = "vcf snp"


@dataclass(frozen=True, kw_only=True)
class VcfIndelParams(_SmallVariantParams):
    """Call indels from a BAM/CRAM into ``indels.vcf.gz``; ploidy as for SNPs."""

    COMMAND = "vcf indel"


def snp(
    params: VcfSnpParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call SNPs; the ``snps`` output carries the record count in its metrics."""
    from wgsextract_cli.commands.vcf.basic import run_small_variants

    return execute(
        params, run_small_variants, progress=progress, cancel=cancel, check=check
    )


def indel(
    params: VcfIndelParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call indels; the ``indels`` output carries the record count in its metrics."""
    from wgsextract_cli.commands.vcf.basic import run_small_variants

    return execute(
        params, run_small_variants, progress=progress, cancel=cancel, check=check
    )


# How ``to_fasta`` applies genotypes (``bcftools consensus`` options).
HAPLOTYPES = ("iupac", "alt", "1", "2")
_OVERRIDE_HELP = "Optional override for VCF input file."
_CONFIDENCE_DEPTH_HELP = (
    "Read depth below LOW labels a call low confidence, below HIGH "
    "moderate (default: config confidence_min_depth 10, "
    "confidence_high_depth 20; halved for haploid calls)"
)
_CONFIDENCE_GQ_HELP = (
    "Genotype quality below LOW labels a call low confidence, below "
    "HIGH moderate (default: config confidence_min_gq 20, "
    "confidence_high_gq 30)"
)


def _vcf_input_param(help: str = _OVERRIDE_HELP) -> Any:
    return param(
        None,
        help=help,
        path="file",
        doc="The VCF to read; wins over ``input``. On the CLI, --vcf-input "
        "defaults to config default_input_vcf when --input isn't given.",
    )


def _resource_param(help: str, config_key: str, **kwargs: Any) -> Any:
    return param(None, help=help, path="file", config_key=config_key, **kwargs)


def _min_score_param(label: str, example: str) -> Any:
    return param(
        None,
        help=f"Minimum {label} score to filter for (e.g., {example}).",
        doc=f"Also write the calls with a {label} score of at least this.",
    )


def _confidence_param(help: str, what: str) -> Any:
    from wgsextract_cli.core.variant_confidence import _non_negative

    return param(
        None,
        help=help,
        cli_type=_non_negative,
        metavar=("LOW", "HIGH"),
        doc=f"(LOW, HIGH): {what} below LOW labels a call low confidence, below "
        "HIGH moderate (default: config confidence_min_* and confidence_high_*).",
    )


def _region_param(help: str = "Chromosomal region") -> Any:
    return param(None, help=help, flags=("-r", "--region"))


@dataclass(frozen=True, kw_only=True)
class _VcfFileParams(Params):
    input: Path | None = optional_input_param(
        "The VCF to read when ``vcf_input`` is unset."
    )
    vcf_input: Path | None = _vcf_input_param()
    outdir: Path | None = outdir_param("the VCF's folder")


@dataclass(frozen=True, kw_only=True)
class _AnnotatorParams(_VcfFileParams):
    ref: Path | None = ref_param()
    threads: int | None = threads_param()


def _check_confidence(params: Any) -> None:
    for name in ("confidence_depth", "confidence_gq"):
        if any(not v >= 0 for v in getattr(params, name) or ()):
            raise UsageError(f"{name} must be non-negative numbers.")


@dataclass(frozen=True, kw_only=True)
class VcfAnnotateParams(_VcfFileParams):
    """Copy columns from an annotation VCF or tab file into ``annotated.vcf.gz``."""

    COMMAND = "vcf annotate"

    vcf_input: Path | None = _vcf_input_param(CLI_HELP["arg_vcf_input"])
    ref: Path | None = ref_param()
    ann_vcf: Path | None = param(
        None,
        help="Annotation VCF file (auto-resolved from --ref if possible)",
        path="file",
    )
    cols: str | None = param(
        None,
        help="Columns to annotate (e.g. ID,INFO/HG)",
        doc="bcftools annotate -c columns; default: inferred from the "
        "annotation file's header.",
    )


@dataclass(frozen=True, kw_only=True)
class VcfFilterParams(_VcfFileParams):
    """Keep the calls in a region or gene, matching an expression, into
    ``filtered.vcf.gz``."""

    COMMAND = "vcf filter"

    vcf_input: Path | None = _vcf_input_param(CLI_HELP["arg_vcf_input"])
    ref: Path | None = ref_param()
    expr: str | None = param(None, help="bcftools filter expression (e.g. 'QUAL>30')")
    gene: str | None = param(None, help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    exclude_near_gaps: bool = param(
        False,
        help="Exclude variants in or near genomic gaps (requires Count Ns output)",
        action="store_true",
    )
    region: str | None = _region_param()


@dataclass(frozen=True, kw_only=True)
class VcfTableParams(_VcfFileParams):
    """One row per variant with its annotations, as ``<stem>_variants.tsv``
    (or ``.parquet``) and an SQLite database."""

    COMMAND = "vcf table"

    vcf_input: Path | None = _vcf_input_param(CLI_HELP["arg_vcf_input"])
    region: str | None = _region_param("Only export this region (e.g. chr22).")
    sample: str | None = param(
        None, help="Sample whose genotypes to use (default: the first)."
    )
    all_alleles: bool = param(
        False,
        help="Also export ALT alleles the sample doesn't carry (0/0 calls, "
        "no-calls, the other ALT of a 0/1 multi-allelic site).",
        action="store_true",
    )
    pharmcat_json: Path | None = param(
        None,
        help="PharmCAT's <stem>.phenotype.json (from 'vcf pharmcat'), to add its "
        "diplotype and phenotype to rows in those genes.",
        metavar="FILE",
        path="file",
    )
    no_sqlite: bool = param(
        False, help="Write only the table file.", action="store_true"
    )
    table_format: str = param(
        "tsv",
        help="Table file format: tsv, or parquet (needs pyarrow) (default: tsv)",
        flags=("--format",),
        choices=FORMATS,
        cli_type=table_format_type(),
    )


@dataclass(frozen=True, kw_only=True)
class VcfToFastaParams(_VcfFileParams):
    """The reference with the VCF's variants applied, as
    ``<stem>_consensus.fa.gz`` (and optionally a liftover chain)."""

    COMMAND = "vcf to-fasta"

    vcf_input: Path | None = _vcf_input_param(CLI_HELP["arg_vcf_input"])
    ref: Path | None = ref_param()
    threads: int | None = threads_param()
    region: str | None = _region_param(
        "Only build this region (e.g. chr21 or 21:30000000-31000000); "
        "default: the whole reference."
    )
    sample: str | None = param(None, help="Sample to use from a multi-sample VCF.")
    haplotype: str = param(
        "iupac",
        help="How genotypes are applied: iupac (default) writes heterozygous "
        "SNVs as IUPAC codes (e.g. R for A/G) and applies homozygous ALTs; alt "
        "applies every ALT; 1 or 2 applies that haplotype of a phased VCF (for "
        "unphased genotypes, the first or second allele, so 1 keeps the "
        "reference at heterozygous sites).",
        choices=HAPLOTYPES,
    )
    uncalled: str = param(
        "auto",
        help="What to write where the VCF has no passing call: n (an N), ref "
        "(the reference base), or auto (default): n for a gVCF, whose reference "
        "blocks say which positions were called, ref for a variant-only VCF.",
        choices=("auto", "n", "ref"),
    )
    include_filtered: bool = param(
        False,
        help="Also apply variants whose FILTER is not PASS.",
        action="store_true",
    )
    mask: Path | None = param(
        None,
        help="BED file of regions to replace with N (e.g. low coverage).",
        path="file",
    )
    chain: bool = param(
        False,
        help="Also write a chain file mapping reference coordinates to the "
        "consensus (indels shift positions).",
        action="store_true",
    )


@dataclass(frozen=True, kw_only=True)
class VcfTrioParams(_VcfFileParams):
    """Calls matching an inheritance mode in a child and both parents, as
    ``trio_<mode>.vcf.gz``. Three VCFs are merged; three BAM/CRAMs are first
    genotyped jointly from the reads (``trio_joint.vcf.gz``), which de novo
    calls need."""

    COMMAND = "vcf trio"

    vcf_input: Path | None = _vcf_input_param(CLI_HELP["arg_vcf_input"])
    ref: Path | None = ref_param()
    threads: int | None = threads_param()
    mother: Path | None = _resource_param(CLI_HELP["arg_mother"], "mother_vcf_path")
    father: Path | None = _resource_param(CLI_HELP["arg_father"], "father_vcf_path")
    proband: Path | None = param(
        None,
        help="The child's VCF or BAM/CRAM; overrides --vcf-input/--input and "
        "configured defaults. With the parents' BAM/CRAMs, genotyped jointly "
        "from the reads, which de novo calls need.",
        path="file",
    )
    mode: str = param(
        "denovo",
        help="Inheritance mode to filter for",
        choices=("denovo", "recessive", "comphet", "all"),
    )
    min_depth: int | None = param(
        None,
        help="BAM/CRAM input: minimum read depth in each person at a reported site "
        "(default 15)",
    )
    proband_sex: str | None = param(
        None,
        help="BAM/CRAM input: the child's sex, for chrX ploidy (default: inferred "
        "from chrX/chrY coverage)",
        choices=("male", "female"),
    )
    region: str | None = _region_param()


@dataclass(frozen=True, kw_only=True)
class VcfClinvarParams(_AnnotatorParams):
    """ClinVar significance and disease (CLNSIG, CLNDN) as
    ``clinvar_annotated.vcf.gz``; the pathogenic and likely pathogenic calls,
    with confidence labels, as ``clinvar_pathogenic.vcf.gz``."""

    COMMAND = "vcf clinvar"

    clinvar_file: Path | None = _resource_param(
        "Path to ClinVar VCF data file.", "clinvar_vcf_path"
    )
    confidence_depth: tuple[float, float] | None = _confidence_param(
        _CONFIDENCE_DEPTH_HELP, "read depth"
    )
    confidence_gq: tuple[float, float] | None = _confidence_param(
        _CONFIDENCE_GQ_HELP, "genotype quality"
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_confidence(self)


@dataclass(frozen=True, kw_only=True)
class VcfRevelParams(_AnnotatorParams):
    """REVEL missense pathogenicity scores as ``revel_annotated.vcf.gz``."""

    COMMAND = "vcf revel"

    revel_file: Path | None = _resource_param(
        "Path to REVEL TSV or VCF data file.", "revel_tsv_path"
    )
    min_score: float | None = _min_score_param("REVEL", "0.5")


@dataclass(frozen=True, kw_only=True)
class VcfPhylopParams(_AnnotatorParams):
    """PhyloP conservation scores as ``phylop_annotated.vcf.gz``."""

    COMMAND = "vcf phylop"

    phylop_file: Path | None = _resource_param(
        "Path to a PhyloP bigWig (phylop_<build>.bw), or a TSV/VCF of scores.",
        "phylop_tsv_path",
    )
    min_score: float | None = _min_score_param("PhyloP", "2.0")


@dataclass(frozen=True, kw_only=True)
class VcfCaddParams(_AnnotatorParams):
    """CADD deleteriousness scores (CADD_RAW, CADD_PHRED) as
    ``cadd_annotated.vcf.gz``."""

    COMMAND = "vcf cadd"

    cadd_file: Path | None = _resource_param(
        "Path to the CADD whole-genome SNV TSV (cadd_<build>.tsv.gz).",
        "cadd_tsv_path",
    )
    cadd_indel_file: Path | None = _resource_param(
        "Path to a CADD indel TSV (cadd_indel_<build>.tsv.gz); used when present.",
        "cadd_indel_tsv_path",
    )
    min_score: float | None = _min_score_param("CADD PHRED", "20")


@dataclass(frozen=True, kw_only=True)
class VcfGerpParams(_AnnotatorParams):
    """GERP++ conservation scores as ``gerp_annotated.vcf.gz``."""

    COMMAND = "vcf gerp"

    gerp_file: Path | None = _resource_param(
        "Path to a GERP++ RS bigWig (gerp_<build>.bw).", "gerp_bigwig_path"
    )
    min_score: float | None = _min_score_param("GERP", "2.0")


@dataclass(frozen=True, kw_only=True)
class VcfGnomadParams(_AnnotatorParams):
    """gnomAD allele frequencies (GNOMAD_AF) as ``gnomad_annotated.vcf.gz``."""

    COMMAND = "vcf gnomad"

    gnomad_file: Path | None = _resource_param(
        "Path to gnomAD data: a sites VCF, or the allele-frequency table "
        "from 'ref gnomad --release 4.1'.",
        "gnomad_vcf_path",
    )
    max_af: float | None = param(
        None,
        help="Maximum Allele Frequency to filter for (e.g., 0.01 for 1%%).",
        doc="Also write the calls rarer than this gnomAD allele frequency "
        "(or absent from gnomAD), with confidence labels.",
    )
    confidence_depth: tuple[float, float] | None = _confidence_param(
        _CONFIDENCE_DEPTH_HELP, "read depth"
    )
    confidence_gq: tuple[float, float] | None = _confidence_param(
        _CONFIDENCE_GQ_HELP, "genotype quality"
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_confidence(self)


@dataclass(frozen=True, kw_only=True)
class VcfSpliceaiParams(_AnnotatorParams):
    """SpliceAI splicing scores as ``spliceai_annotated.vcf.gz``."""

    COMMAND = "vcf spliceai"

    spliceai_file: Path | None = _resource_param(
        "Path to SpliceAI VCF data file.", "spliceai_vcf_path"
    )


@dataclass(frozen=True, kw_only=True)
class VcfAlphamissenseParams(_AnnotatorParams):
    """AlphaMissense pathogenicity scores as ``alphamissense_annotated.vcf.gz``."""

    COMMAND = "vcf alphamissense"

    am_file: Path | None = _resource_param(
        "Path to AlphaMissense VCF data file.", "alphamissense_vcf_path"
    )
    min_score: float | None = _min_score_param("AlphaMissense", "0.5")


@dataclass(frozen=True, kw_only=True)
class VcfPharmgkbParams(_AnnotatorParams):
    """PharmGKB drug-response annotations as ``pharmgkb_annotated.vcf.gz``
    (GRCh38 only)."""

    COMMAND = "vcf pharmgkb"

    pharmgkb_file: Path | None = _resource_param(
        "Path to PharmGKB VCF or data file.", "pharmgkb_vcf_path"
    )


@dataclass(frozen=True, kw_only=True)
class VcfPharmcatParams(_VcfFileParams):
    """Star-allele diplotypes and drug-response phenotypes from a GRCh38 VCF
    with PharmCAT: ``<stem>.report.html``, ``.report.json`` and
    ``.phenotype.json``."""

    COMMAND = "vcf pharmcat"

    ref: Path | None = ref_param()
    pharmcat_dir: Path | None = param(
        None,
        help="Extracted PharmCAT pipeline release (default: <reference "
        "library>/pharmcat).",
        path="directory",
        config_key="pharmcat_directory",
        local_only=True,
    )
    absent_to_ref: bool = param(
        False,
        help="Treat positions absent from the VCF as reference. Only for a "
        "whole-genome VCF that lists variants only (not a gVCF).",
        action="store_true",
    )
    outside_call: tuple[Path, ...] | None = param(
        None,
        help="PharmCAT outside-call file (gene<TAB>diplotype), e.g. from "
        "`typing cyp2d6` or `typing hla`. Repeatable; one call per gene.",
        metavar="FILE",
        path="file",
        action="append",
    )
    confidence_depth: tuple[float, float] | None = _confidence_param(
        _CONFIDENCE_DEPTH_HELP, "read depth"
    )
    confidence_gq: tuple[float, float] | None = _confidence_param(
        _CONFIDENCE_GQ_HELP, "genotype quality"
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_confidence(self)


@dataclass(frozen=True, kw_only=True)
class VcfChainAnnotateParams(_VcfFileParams):
    """Several annotations applied in turn, as ``chain_annotated.vcf.gz``;
    steps whose tool or data is missing are skipped with a warning."""

    COMMAND = "vcf chain-annotate"

    ref: Path | None = ref_param()
    annotations: str = param(
        "clinvar,revel,phylop,gnomad,vep",
        help="Comma-separated list of annotations to apply in order (default: "
        "clinvar,revel,phylop,gnomad,vep).",
    )
    keep_intermediates: bool = param(
        False,
        help="Keep intermediate VCF files generated during the chain process.",
        action="store_true",
    )


_P = TypeVar("_P", bound=_VcfFileParams)


def _run_on_vcf(
    params: _P,
    impl: Callable[[_P], None],
    progress: ProgressCallback | None,
    cancel: CancelToken | None,
    check: bool,
) -> CommandResult:
    if params.vcf_input is not None:
        # Only the chosen VCF is read and recorded, as the input.
        params = dataclasses.replace(params, input=params.vcf_input, vcf_input=None)
    return execute(params, impl, progress=progress, cancel=cancel, check=check)


def annotate(
    params: VcfAnnotateParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate; output role ``annotated``, with the record count in its metrics."""
    from wgsextract_cli.commands.vcf.basic import run_annotate

    return _run_on_vcf(params, run_annotate, progress, cancel, check)


def filter(
    params: VcfFilterParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Filter; output role ``filtered_vcf``, with the record count in its metrics."""
    from wgsextract_cli.commands.vcf.filter_trio import run_filter

    return _run_on_vcf(params, run_filter, progress, cancel, check)


def table(
    params: VcfTableParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the table (role ``variant_table``) and database (``variant_database``);
    ``rows`` is a metric."""
    from wgsextract_cli.commands.vcf.table import run_table

    return _run_on_vcf(params, run_table, progress, cancel, check)


def to_fasta(
    params: VcfToFastaParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Build the consensus (role ``consensus_fasta``, and ``liftover_chain`` with
    ``chain``)."""
    from wgsextract_cli.commands.vcf.consensus import run_to_fasta

    return _run_on_vcf(params, run_to_fasta, progress, cancel, check)


def trio(
    params: VcfTrioParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Filter the trio; output roles ``trio_<mode>`` (and ``trio_joint``,
    ``trio_denovo_isolated`` from BAM/CRAMs)."""
    from wgsextract_cli.commands.vcf.filter_trio import run_trio

    return _run_on_vcf(params, run_trio, progress, cancel, check)


def clinvar(
    params: VcfClinvarParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with ClinVar; output roles ``annotated_vcf`` and
    ``clinvar_pathogenic``."""
    from wgsextract_cli.commands.vcf.clinvar_revel import run_clinvar

    return _run_on_vcf(params, run_clinvar, progress, cancel, check)


def revel(
    params: VcfRevelParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with REVEL; output roles ``annotated_vcf`` and, with ``min_score``,
    ``filtered_vcf``."""
    from wgsextract_cli.commands.vcf.clinvar_revel import run_revel

    return _run_on_vcf(params, run_revel, progress, cancel, check)


def phylop(
    params: VcfPhylopParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with PhyloP; output roles ``annotated_vcf`` and, with ``min_score``,
    ``filtered_vcf``."""
    from wgsextract_cli.commands.vcf.population import run_phylop

    return _run_on_vcf(params, run_phylop, progress, cancel, check)


def cadd(
    params: VcfCaddParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with CADD; output roles ``annotated_vcf`` and, with ``min_score``,
    ``filtered_vcf``."""
    from wgsextract_cli.commands.vcf.deleteriousness import run_cadd

    return _run_on_vcf(params, run_cadd, progress, cancel, check)


def gerp(
    params: VcfGerpParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with GERP++; output roles ``annotated_vcf`` and, with ``min_score``,
    ``filtered_vcf``."""
    from wgsextract_cli.commands.vcf.deleteriousness import run_gerp

    return _run_on_vcf(params, run_gerp, progress, cancel, check)


def gnomad(
    params: VcfGnomadParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with gnomAD; output roles ``annotated_vcf`` and, with ``max_af``,
    ``rare_variants``."""
    from wgsextract_cli.commands.vcf.population import run_gnomad

    return _run_on_vcf(params, run_gnomad, progress, cancel, check)


def spliceai(
    params: VcfSpliceaiParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with SpliceAI; output role ``annotated_vcf``."""
    from wgsextract_cli.commands.vcf.splicing import run_spliceai

    return _run_on_vcf(params, run_spliceai, progress, cancel, check)


def alphamissense(
    params: VcfAlphamissenseParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with AlphaMissense; output roles ``annotated_vcf`` and, with
    ``min_score``, ``filtered_vcf``."""
    from wgsextract_cli.commands.vcf.splicing import run_alphamissense

    return _run_on_vcf(params, run_alphamissense, progress, cancel, check)


def pharmgkb(
    params: VcfPharmgkbParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate with PharmGKB; output role ``annotated_vcf``."""
    from wgsextract_cli.commands.vcf.splicing import run_pharmgkb

    return _run_on_vcf(params, run_pharmgkb, progress, cancel, check)


def pharmcat(
    params: VcfPharmcatParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Run PharmCAT; output roles ``pharmcat_report``, ``pharmcat_report_json`` and
    ``pharmcat_phenotypes``."""
    from wgsextract_cli.commands.vcf.pharmcat import run_pharmcat

    return _run_on_vcf(params, run_pharmcat, progress, cancel, check)


def chain_annotate(
    params: VcfChainAnnotateParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Run the chain; output role ``annotated_vcf``; ``annotations_applied`` is a
    metric."""
    from wgsextract_cli.commands.vcf.chain import run_chain_annotate

    return _run_on_vcf(params, run_chain_annotate, progress, cancel, check)


__all__ = [
    "HAPLOTYPES",
    "VcfAlphamissenseParams",
    "VcfAnnotateParams",
    "VcfCaddParams",
    "VcfChainAnnotateParams",
    "VcfClinvarParams",
    "VcfFilterParams",
    "VcfGerpParams",
    "VcfGnomadParams",
    "VcfIndelParams",
    "VcfPharmcatParams",
    "VcfPharmgkbParams",
    "VcfPhylopParams",
    "VcfRevelParams",
    "VcfSnpParams",
    "VcfSpliceaiParams",
    "VcfTableParams",
    "VcfToFastaParams",
    "VcfTrioParams",
    "alphamissense",
    "annotate",
    "cadd",
    "chain_annotate",
    "clinvar",
    "filter",
    "gerp",
    "gnomad",
    "indel",
    "pharmcat",
    "pharmgkb",
    "phylop",
    "revel",
    "snp",
    "spliceai",
    "table",
    "to_fasta",
    "trio",
]
