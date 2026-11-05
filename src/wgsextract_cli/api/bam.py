"""``bam``: sort, index, convert, merge and unalign BAM/CRAM files.

Outputs are named after the input (``sample.bam`` gives ``sample_sorted.bam``)
and written to ``outdir``, else beside the input. A CRAM is read with the
matching reference: ``ref``, else the one the input's header names in the
reference library."""

from __future__ import annotations

from dataclasses import MISSING, dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    input_param,
    memory_param,
    optional_input_param,
    outdir_param,
    ref_param,
    threads_param,
)

# SAM-spec @RG PL values.
UBAM_PLATFORMS = (
    "ILLUMINA",
    "DNBSEQ",
    "ELEMENT",
    "ULTIMA",
    "PACBIO",
    "ONT",
    "IONTORRENT",
    "LS454",
    "SOLID",
    "CAPILLARY",
)

_INPUT_DOC = "BAM or CRAM file."
_GENE_HELP = "Filter by Gene Name (e.g. BRCA1, KCNQ2)"
_GENE_DOC = (
    "A gene name (e.g. BRCA1) to limit the output to, looked up in the reference "
    "library's gene map (hg19/hg38 only); the region wins when both are set."
)


def _region_param() -> Any:
    return param(
        None,
        help=CLI_HELP["arg_region"],
        flags=("-r", "--region"),
        doc="Region (chrY:10000-20000), comma-separated regions, or a BED file "
        "to limit the output to.",
        may_name_file=True,
    )


def _gene_param() -> Any:
    return param(None, help=_GENE_HELP, doc=_GENE_DOC)


def _fastq_param(help: str, doc: str, required: bool) -> Any:
    """An output FASTQ: a bare name goes in outdir, a path is used as given."""
    return param(
        MISSING if required else None,
        help=help,
        doc=doc + " to write: a bare name goes in outdir, a path is used as given.",
        may_name_file=True,
    )


@dataclass(frozen=True, kw_only=True)
class _InputParams(Params):
    input: Path = input_param(_INPUT_DOC)


@dataclass(frozen=True, kw_only=True)
class _ConvertParams(_InputParams):
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()


@dataclass(frozen=True, kw_only=True)
class BamSortParams(_ConvertParams):
    """Coordinate-sort and index the input as ``<input>_sorted.bam`` (or
    ``.cram``), optionally limited to a region or gene. A whole-file sort first
    checks that the temporary and final files fit on disk."""

    COMMAND = "bam sort"

    memory: str | None = memory_param()
    region: str | None = _region_param()
    gene: str | None = _gene_param()


@dataclass(frozen=True, kw_only=True)
class BamIndexParams(_InputParams):
    """Index the input in place (``.bai`` or ``.crai``)."""

    COMMAND = "bam index"


@dataclass(frozen=True, kw_only=True)
class BamUnindexParams(_InputParams):
    """Delete the input's index files (``.bai``, ``.crai``, ``.csi``)."""

    COMMAND = "bam unindex"


@dataclass(frozen=True, kw_only=True)
class BamUnsortParams(_InputParams):
    """A copy marked unsorted (``SO:unsorted``) as ``<input>_unsorted.bam``
    (or ``.cram``); the reads are unchanged."""

    COMMAND = "bam unsort"

    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()


@dataclass(frozen=True, kw_only=True)
class BamToCramParams(_ConvertParams):
    """Convert to an indexed ``<input>.cram``, optionally limited to a region
    or gene."""

    COMMAND = "bam to-cram"

    region: str | None = _region_param()
    gene: str | None = _gene_param()
    cram_version: str = param(
        "3.0",
        help="CRAM version to output (default: 3.0 for better compatibility)",
        choices=("2.1", "3.0", "3.1"),
    )


@dataclass(frozen=True, kw_only=True)
class BamToBamParams(_ConvertParams):
    """Convert to an indexed ``<input>.bam``, optionally limited to a region or
    gene."""

    COMMAND = "bam to-bam"

    region: str | None = _region_param()
    gene: str | None = _gene_param()


@dataclass(frozen=True, kw_only=True)
class BamUnalignParams(_ConvertParams):
    """Write the reads back to paired FASTQs, name-sorted so mates line up.
    Reads without a mate go to ``se`` when set. Warns when the input has more
    than one read group, which FASTQ can't keep apart."""

    COMMAND = "bam unalign"

    memory: str | None = memory_param()
    r1: str = _fastq_param(
        "Read 1 FASTQ to write (a bare name goes in --outdir)", "Read 1 FASTQ", True
    )
    r2: str = _fastq_param(
        "Read 2 FASTQ to write (a bare name goes in --outdir)", "Read 2 FASTQ", True
    )
    se: str | None = _fastq_param(
        "FASTQ for reads without a mate (a bare name goes in --outdir)",
        "FASTQ for reads without a mate",
        False,
    )
    region: str | None = _region_param()
    gene: str | None = _gene_param()


@dataclass(frozen=True, kw_only=True)
class BamIdentifyParams(_InputParams):
    """Identify the reference the input was aligned to: the ``reference_md5_signature``
    metric is the MD5 of its sequence names and lengths."""

    COMMAND = "bam identify"

    ref: Path | None = ref_param()


@dataclass(frozen=True, kw_only=True)
class BamMergeParams(Params):
    """Merge coordinate-sorted BAM/CRAM files aligned to the same reference
    into one indexed file. Identical ``@RG`` lines in several inputs are one
    read group; conflicting ones get distinct IDs, with a warning."""

    COMMAND = "bam merge"

    inputs: tuple[Path, ...] = param(
        help="Coordinate-sorted BAM/CRAM files aligned to the same reference.",
        metavar="FILE",
        path="file",
        positional=True,
        required_message="bam merge needs at least two input files.",
        doc="Two or more coordinate-sorted BAM/CRAM files aligned to the same "
        "reference.",
    )
    output: str | None = param(
        None,
        help="Output file name or path (a bare name goes in --outdir; "
        "default: merged.bam, or merged.cram with --cram).",
        doc="Output file name or path; a bare name goes in outdir (default: "
        "merged.bam, or merged.cram with cram).",
        may_name_file=True,
    )
    cram: bool = param(False, help="Write CRAM instead of BAM.", action="store_true")
    attach_rg: bool = param(
        False,
        help="Tag each read with a read group named after its input file, for "
        "inputs without @RG lines (so reads stay traceable to their lane).",
        action="store_true",
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param("the first input's folder")
    threads: int | None = threads_param()


@dataclass(frozen=True, kw_only=True)
class BamToUbamParams(Params):
    """A queryname-sorted unaligned BAM, as GATK's pipelines start from, as
    ``<input>_unaligned.bam``: from an aligned ``input`` (read-describing tags
    kept, alignments dropped) or from FASTQ ``r1`` and optional ``r2``. Input
    without read groups gets one from ``sample``, ``library`` and
    ``platform``, with the flowcell and lane parsed from the read names."""

    COMMAND = "bam to-ubam"

    input: Path | None = optional_input_param(
        "Aligned BAM or CRAM file; set it or r1, not both."
    )
    r1: Path | None = param(
        None,
        help="Read 1 FASTQ (instead of an aligned --input BAM/CRAM).",
        path="file",
    )
    r2: Path | None = param(None, help="Read 2 FASTQ for paired-end --r1.", path="file")
    sample: str | None = param(
        None,
        help="Sample name (SM) for FASTQ input, or for a BAM/CRAM without @RG "
        "lines (default: the input file name).",
    )
    library: str = param("lib1", help="Library name (LB) for a new read group.")
    platform: str | None = param(
        None,
        help="Sequencing platform (PL) for a new read group (default: detected "
        "from the read names).",
        choices=UBAM_PLATFORMS,
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    memory: str | None = memory_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if (self.input is None) == (self.r1 is None):
            raise UsageError(
                "Pass either --input (aligned BAM/CRAM) or --r1 [--r2] (FASTQ)."
            )
        if self.r2 is not None and self.r1 is None:
            raise UsageError("--r2 needs --r1.")


def sort(
    params: BamSortParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Coordinate-sort and index (output role ``sorted_alignment``)."""
    from wgsextract_cli.commands.bam import run_sort

    return execute(params, run_sort, progress=progress, cancel=cancel, check=check)


def index(
    params: BamIndexParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Index the input (output role ``alignment_index``)."""
    from wgsextract_cli.commands.bam import run_index

    return execute(params, run_index, progress=progress, cancel=cancel, check=check)


def unindex(
    params: BamUnindexParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Delete the input's indexes (the ``removed_indexes`` metric counts them)."""
    from wgsextract_cli.commands.bam import run_unindex

    return execute(params, run_unindex, progress=progress, cancel=cancel, check=check)


def unsort(
    params: BamUnsortParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the copy marked unsorted (output role ``unsorted_alignment``)."""
    from wgsextract_cli.commands.bam import run_unsort

    return execute(params, run_unsort, progress=progress, cancel=cancel, check=check)


def to_cram(
    params: BamToCramParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Convert to CRAM (output role ``alignment``)."""
    from wgsextract_cli.commands.bam import run_to_cram

    return execute(params, run_to_cram, progress=progress, cancel=cancel, check=check)


def to_bam(
    params: BamToBamParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Convert to BAM (output role ``alignment``)."""
    from wgsextract_cli.commands.bam import run_to_bam

    return execute(params, run_to_bam, progress=progress, cancel=cancel, check=check)


def unalign(
    params: BamUnalignParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write FASTQs (output roles ``fastq_r1``, ``fastq_r2`` and, with ``se``,
    ``fastq_single``; the ``read_groups`` metric counts the input's)."""
    from wgsextract_cli.commands.bam import run_unalign

    return execute(params, run_unalign, progress=progress, cancel=cancel, check=check)


def identify(
    params: BamIdentifyParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Identify the reference (the ``reference_md5_signature`` metric)."""
    from wgsextract_cli.commands.bam import run_identify

    return execute(params, run_identify, progress=progress, cancel=cancel, check=check)


def merge(
    params: BamMergeParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Merge the inputs (output role ``merged_alignment``; the
    ``merged_inputs`` metric counts them)."""
    from wgsextract_cli.commands.bam import run_merge

    return execute(params, run_merge, progress=progress, cancel=cancel, check=check)


def to_ubam(
    params: BamToUbamParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the unaligned BAM (output role ``unaligned_bam``)."""
    from wgsextract_cli.commands._bam_ubam import run_to_ubam

    return execute(params, run_to_ubam, progress=progress, cancel=cancel, check=check)


__all__ = [
    "UBAM_PLATFORMS",
    "BamIdentifyParams",
    "BamIndexParams",
    "BamMergeParams",
    "BamSortParams",
    "BamToBamParams",
    "BamToCramParams",
    "BamToUbamParams",
    "BamUnalignParams",
    "BamUnindexParams",
    "BamUnsortParams",
    "identify",
    "index",
    "merge",
    "sort",
    "to_bam",
    "to_cram",
    "to_ubam",
    "unalign",
    "unindex",
    "unsort",
]
