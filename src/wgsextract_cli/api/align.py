"""``align`` and ``realign``: map reads to a reference as a sorted, indexed,
duplicate-marked BAM or CRAM.

Short reads go through bwa-mem2 or bwa and samblaster, long reads through
pbmm2 (PacBio) or minimap2, and Ultima reads through their own pipeline. The
output is named after the reads (``sample_R1.fastq.gz`` and
``sample_R2.fastq.gz`` give ``sample_aligned.bam``) and written to
``outdir``, else beside the reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    dry_run_param,
    force_steps_param,
    input_param,
    memory_param,
    optional_input_param,
    outdir_param,
    ref_param,
    restart_param,
    threads_param,
)

PLATFORMS = ("illumina", "dnbseq", "ultima", "pacbio", "hifi", "clr", "ont", "nanopore")
ALIGNERS = ("auto", "bwa", "bwa-mem2", "minimap2", "pbmm2")
PRESETS = ("sr", "map-pb", "map-hifi", "map-ont", "ccs", "subread")
REALIGN_STEPS = ("extract", "align", "index")

GPU_HELP = (
    "Align on an NVIDIA GPU with Parabricks: BWA-MEM for short reads, "
    "minimap2 for HiFi (Linux, Docker; see README). ONT requires CPU "
    "alignment to retain MM/ML methylation tags."
)
_FORMAT_DOC = "Output format: BAM or CRAM (written with the target reference)."


@dataclass(frozen=True, kw_only=True)
class AlignParams(Params):
    """Align FASTQ reads (or an unaligned BAM) to ``ref`` as
    ``<reads stem>_aligned.bam`` (or ``.cram``) with its index. ``r1`` is
    required; the CLI's ``--genome`` may fill it in from a sample workspace."""

    COMMAND = "align"

    r1: Path | None = param(
        None,
        help=CLI_HELP["arg_r1"],
        doc="Read 1 FASTQ (or an unaligned BAM/CRAM of the reads).",
        path="file",
    )
    r2: Path | None = param(
        None, help=CLI_HELP["arg_r2"], doc="Read 2 FASTQ, for pairs.", path="file"
    )
    preset: str | None = param(
        None,
        help="minimap2 preset for non-BWA alignment. Defaults to map-hifi for "
        "PacBio and map-ont for ONT.",
        choices=PRESETS,
    )
    platform: str | None = param(
        None,
        help="Sequencing platform; selects platform-specific alignment tooling "
        "(default: detected from read groups or read names, else illumina).",
        choices=PLATFORMS,
    )
    aligner: str = param(
        "auto",
        help="Aligner to use. Auto uses bwa-mem2 for short reads when the reference "
        "has its index (see `ref index`), else bwa; for long reads pbmm2 for PacBio "
        "when installed, otherwise minimap2.",
        choices=ALIGNERS,
    )
    gpu: bool = param(False, help=GPU_HELP, action="store_true")
    sample: str | None = param(
        None, help="Sample name for the read group (default: the reads' file name)."
    )
    long_read: bool = param(
        False,
        help=CLI_HELP["arg_long_read"],
        doc="Treat the reads as long reads (PacBio/ONT) and align them with "
        "pbmm2 or minimap2.",
        action="store_true",
    )
    format: str = param(
        "BAM", help="Output format", doc=_FORMAT_DOC, choices=("BAM", "CRAM")
    )
    input: Path | None = optional_input_param(
        "An aligned BAM/CRAM of the same sample whose header picks the "
        "reference from the library when ref is unset."
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param("the input's folder, else the reads'")
    threads: int | None = threads_param()
    memory: str | None = memory_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.r1 is None:
            # Optional on the command line, where --genome may find the reads.
            raise UsageError("--r1 is required unless --genome resolves read inputs.")


@dataclass(frozen=True, kw_only=True)
class RealignParams(Params):
    """Re-align a BAM/CRAM to ``ref`` (possibly a different build) as
    ``<input stem>_aligned.bam`` (or ``.cram``) with its index.

    The reads are extracted to FASTQ first, or streamed into the aligner when
    the machine has the memory; FASTQs from an earlier extraction (beside the
    input or in ``outdir``) or given as ``r1``/``r2`` are reused. Free disk
    space is checked first. Completed steps (extract, align, index) are
    recorded in ``<output>.steps.json`` and skipped on a rerun whose inputs,
    settings and tools are unchanged."""

    COMMAND = "realign"

    r1: Path | None = param(
        None,
        help=CLI_HELP["arg_r1"],
        doc="Read 1 FASTQ already extracted from the input, to align instead "
        "of extracting it.",
        path="file",
    )
    r2: Path | None = param(
        None, help=CLI_HELP["arg_r2"], doc="Read 2 FASTQ, with r1.", path="file"
    )
    aligner: str = param(
        "auto",
        help="Aligner to use (matches the 'align' command).",
        doc="Aligner: auto, bwa, bwa-mem2, minimap2 or pbmm2, chosen as for align.",
        choices=ALIGNERS,
    )
    gpu: bool = param(False, help=GPU_HELP, action="store_true")
    platform: str | None = param(
        None,
        help="Sequencing platform (default: detected from read groups or read "
        "names, else illumina).",
        choices=PLATFORMS,
    )
    preset: str | None = param(None, help="minimap2/pbmm2 preset.", choices=PRESETS)
    input_ref: Path | None = param(
        None,
        help="Reference FASTA the input CRAM was encoded with (default: --ref "
        "when its sequence names, lengths and MD5s match the CRAM, else the "
        "FASTA in the reference library whose sequence dictionary matches). "
        "--ref is the reference to realign to.",
        path="file",
    )
    sample: str | None = param(
        None, help="Sample name to embed in read-group metadata."
    )
    long_read: bool = param(
        False, help="Treat input as long-read data (PacBio/ONT).", action="store_true"
    )
    format: str = param(
        "BAM", help="Output format.", doc=_FORMAT_DOC, choices=("BAM", "CRAM")
    )
    keep_fastq: bool = param(
        False,
        help="Keep the FASTQs extracted from --input after alignment (default: "
        "delete them). FASTQs passed with --r1/--r2 or reused from an earlier "
        "extraction are never deleted.",
        action="store_true",
    )
    force_extract: bool = param(
        False,
        help="Re-extract FASTQs even if a matching pair already exists "
        "next to the BAM.",
        action="store_true",
    )
    no_preflight: bool = param(
        False,
        help="Skip the up-front disk space check (not recommended).",
        action="store_true",
    )
    restart: bool = restart_param()
    force_steps: tuple[str, ...] | None = force_steps_param(REALIGN_STEPS)
    dry_run: bool = dry_run_param()
    input: Path = input_param(
        "The aligned BAM or CRAM to realign.",
        required_message="--input <BAM|CRAM> is required for realign. Pass it "
        "before the subcommand or via --input.",
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    memory: str | None = memory_param()


def align(
    params: AlignParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Align reads to a reference (see ``AlignParams``)."""
    from wgsextract_cli.commands.align import run_align

    return execute(params, run_align, progress=progress, cancel=cancel, check=check)


def realign(
    params: RealignParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Re-align a BAM/CRAM to a reference (see ``RealignParams``)."""
    from wgsextract_cli.commands.realign import run_realign

    return execute(params, run_realign, progress=progress, cancel=cancel, check=check)
