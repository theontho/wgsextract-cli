"""``pet-align``: align a dog's or cat's reads and call its variants.

The reads are aligned with bwa-mem2 or bwa (as ``align`` does) to the
species' reference, then ``bcftools mpileup | call`` writes the variants.
The outputs, ``<reads stem>_<species>.bam`` (or ``.cram``) and
``<reads stem>_<species>.vcf.gz``, go to ``outdir``, else beside the reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import memory_param, outdir_param, ref_param, threads_param

SPECIES = ("dog", "cat")


@dataclass(frozen=True, kw_only=True)
class PetAlignParams(Params):
    """Align a dog's or cat's FASTQ reads to ``ref`` and call its variants.
    ``ref`` is the species' FASTA, or a reference library whose ``genomes``
    folder holds the NCBI reference (``ref library --install``). ``r1`` is
    required; the CLI's ``--genome`` may fill it in from a sample workspace."""

    COMMAND = "pet-align"

    r1: Path | None = param(
        None, help=CLI_HELP["arg_r1"], doc="Read 1 FASTQ.", path="file"
    )
    r2: Path | None = param(
        None, help=CLI_HELP["arg_r2"], doc="Read 2 FASTQ, for pairs.", path="file"
    )
    species: str = param(help="Species for analysis", choices=SPECIES)
    format: str = param(
        "BAM",
        help="Output format",
        doc="Output format of the alignment: BAM or CRAM.",
        choices=("BAM", "CRAM"),
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param("the reads' folder")
    threads: int | None = threads_param()
    memory: str | None = memory_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.r1 is None:
            # Optional on the command line, where --genome may find the reads.
            raise UsageError("--r1 is required unless --genome resolves FASTQ inputs.")


def align(
    params: PetAlignParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Align a pet's reads and call its variants (see ``PetAlignParams``)."""
    from wgsextract_cli.commands.pet import run_pet

    return execute(params, run_pet, progress=progress, cancel=cancel, check=check)
