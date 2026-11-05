"""``extract``: pull chromosomes, regions or unmapped reads out of a BAM/CRAM
into smaller BAM, VCF or FASTA files.

Outputs are named after the input (``sample.bam`` gives ``sample_Y.bam``) and
written to ``outdir``, else beside the input. Calling and consensus commands
(``mito_fasta``, ``mito_vcf``, ``ydna_vcf``) need the matching reference; the
others use it only to decode a CRAM."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import input_param, memory_param, outdir_param, ref_param, threads_param

_GENE_HELP = "Filter by Gene Name (e.g. BRCA1, KCNQ2)"
_GENE_DOC = (
    "A gene name (e.g. BRCA1) to extract, looked up in the reference library's "
    "gene map (hg19/hg38 only); the region wins when both are set."
)


@dataclass(frozen=True, kw_only=True)
class _ExtractParams(Params):
    input: Path = input_param("BAM or CRAM to extract from.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    memory: str | None = memory_param()


@dataclass(frozen=True, kw_only=True)
class MitoFastaParams(_ExtractParams):
    """The mitochondrial consensus sequence as ``<input>_MT.fasta``: chrM
    reads called haploid with bcftools and applied to the reference's chrM."""

    COMMAND = "extract mito-fasta"


@dataclass(frozen=True, kw_only=True)
class MitoVcfParams(_ExtractParams):
    """Mitochondrial variants as ``<input>_MT.vcf.gz``, called haploid."""

    COMMAND = "extract mito-vcf"


@dataclass(frozen=True, kw_only=True)
class MtBamParams(_ExtractParams):
    """The chrM reads as ``<input>_mtDNA.bam``."""

    COMMAND = "extract mt-bam"


@dataclass(frozen=True, kw_only=True)
class BamSubsetParams(_ExtractParams):
    """A random fraction of the reads as ``<input>_subset.bam``, optionally
    limited to a region or gene."""

    COMMAND = "extract bam-subset"

    fraction: float = param(
        help=CLI_HELP["arg_fraction"],
        flags=("--fraction", "-f"),
        required_message="--fraction is required.",
        doc="Fraction of the reads to keep, e.g. 0.1 for 10%.",
    )
    region: str | None = param(
        None,
        help=CLI_HELP["arg_region"],
        flags=("-r", "--region"),
        may_name_file=True,
    )
    gene: str | None = param(None, help=_GENE_HELP, doc=_GENE_DOC)


@dataclass(frozen=True, kw_only=True)
class YdnaBamParams(_ExtractParams):
    """The chrY reads as ``<input>_Y.bam`` (``<input>_Y_poz.bam`` with ``poz``).
    Warns when the sample appears female, whose chrY reads are mismapped."""

    COMMAND = "extract ydna-bam"

    poz: bool = param(
        False,
        help="Restrict Y reads to the bundled CombBED/McDonald/Poznik union "
        "(GRCh37/38 only).",
        action="store_true",
    )


@dataclass(frozen=True, kw_only=True)
class YdnaVcfParams(_ExtractParams):
    """Y-chromosome variants as ``<input>_Y.vcf.gz``, called haploid, or with
    ``ybrowse`` the installed yBrowse SNP targets as ``<input>_Y_ybrowse.vcf.gz``."""

    COMMAND = "extract ydna-vcf"

    ybrowse: bool = param(
        False,
        help="Call installed, allele-annotated yBrowse SNP targets (GRCh38 only; "
        "no downloads).",
        action="store_true",
    )


@dataclass(frozen=True, kw_only=True)
class YMtExtractParams(_ExtractParams):
    """The chrY and chrM reads in one ``<input>_Y_MT.bam``."""

    COMMAND = "extract y-mt-extract"


@dataclass(frozen=True, kw_only=True)
class UnmappedParams(_ExtractParams):
    """The unmapped reads as ``<input>_unmapped.bam``."""

    COMMAND = "extract unmapped"

    fastq: bool = param(
        False,
        help="Also write paired, single-end and singleton unmapped FASTQs.",
        action="store_true",
    )


@dataclass(frozen=True, kw_only=True)
class CustomParams(_ExtractParams):
    """The reads in a region, a comma-separated list of regions, a BED file or a
    gene as ``<input>_<region>.bam``. ``region`` or ``gene`` is required."""

    COMMAND = "extract custom"

    region: str | None = param(
        None,
        help=CLI_HELP["arg_region"],
        flags=("-r", "--region"),
        doc="Region (chrY:10000-20000), comma-separated regions, or a BED file.",
        may_name_file=True,
    )
    gene: str | None = param(None, help=_GENE_HELP, doc=_GENE_DOC)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not (self.region or self.gene):
            raise UsageError("--region or --gene is required for custom extraction.")


def mito_fasta(
    params: MitoFastaParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the mitochondrial consensus FASTA (output role ``mt_fasta``)."""
    from wgsextract_cli.commands.extract import run_mito_fasta

    return execute(
        params, run_mito_fasta, progress=progress, cancel=cancel, check=check
    )


def mito_vcf(
    params: MitoVcfParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call mitochondrial variants (output role ``mt_vcf``; the ``variants``
    metric counts them)."""
    from wgsextract_cli.commands.extract import run_mito_vcf

    return execute(params, run_mito_vcf, progress=progress, cancel=cancel, check=check)


def mt_bam(
    params: MtBamParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Extract the chrM reads (output role ``mt_bam``)."""
    from wgsextract_cli.commands.extract import run_mt_bam

    return execute(params, run_mt_bam, progress=progress, cancel=cancel, check=check)


def bam_subset(
    params: BamSubsetParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Subsample the reads (output role ``subset_bam``)."""
    from wgsextract_cli.commands.extract import run_bam_subset

    return execute(
        params, run_bam_subset, progress=progress, cancel=cancel, check=check
    )


def ydna_bam(
    params: YdnaBamParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Extract the chrY reads (output role ``y_bam``)."""
    from wgsextract_cli.commands.extract import run_ydna_bam

    return execute(params, run_ydna_bam, progress=progress, cancel=cancel, check=check)


def ydna_vcf(
    params: YdnaVcfParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call Y-chromosome variants (output role ``y_vcf``; the ``variants``
    metric counts them)."""
    from wgsextract_cli.commands.extract import run_ydna_vcf

    return execute(params, run_ydna_vcf, progress=progress, cancel=cancel, check=check)


def y_mt_extract(
    params: YMtExtractParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Extract the chrY and chrM reads together (output role ``y_mt_bam``)."""
    from wgsextract_cli.commands.extract import run_y_mt_extract

    return execute(
        params, run_y_mt_extract, progress=progress, cancel=cancel, check=check
    )


def unmapped(
    params: UnmappedParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Extract the unmapped reads (output role ``unmapped_bam``; with ``fastq``
    also ``fastq_r1``, ``fastq_r2``, ``fastq_other`` and ``fastq_singletons``
    for the FASTQs that have reads)."""
    from wgsextract_cli.commands.extract import run_unmapped

    return execute(params, run_unmapped, progress=progress, cancel=cancel, check=check)


def custom(
    params: CustomParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Extract the reads of a region, BED file or gene (output role
    ``region_bam``)."""
    from wgsextract_cli.commands.extract import run_custom

    return execute(params, run_custom, progress=progress, cancel=cancel, check=check)


__all__ = [
    "BamSubsetParams",
    "CustomParams",
    "MitoFastaParams",
    "MitoVcfParams",
    "MtBamParams",
    "UnmappedParams",
    "YMtExtractParams",
    "YdnaBamParams",
    "YdnaVcfParams",
    "bam_subset",
    "custom",
    "mito_fasta",
    "mito_vcf",
    "mt_bam",
    "unmapped",
    "y_mt_extract",
    "ydna_bam",
    "ydna_vcf",
]
