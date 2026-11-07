"""``methylation``: base modifications (5mC/5hmC) from long-read MM/ML tags.

``pileup`` runs ONT's modkit over a coordinate-sorted, indexed Nanopore or
PacBio BAM/CRAM whose reads carry MM/ML tags, against the reference they
were aligned to. It writes ``<input stem>.<context>.bedmethyl.gz`` (+ .tbi)
and a ``.methylation.tsv`` (or ``.parquet``) summary in ``outdir``, else
beside the input; with ``table_format="parquet"`` the sites are also written
as ``.bedmethyl.parquet``. The result's metrics give the platform, context
and, per modification, the sites, median coverage and weighted percentage
modified. modkit is optional (``pixi install -e modkit``) and unavailable on
Windows.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.table_writer import FORMATS, table_format_type

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import input_param, outdir_param, ref_param, threads_param


@dataclass(frozen=True, kw_only=True)
class MethylationPileupParams(Params):
    """Tabulate 5mC (and optionally 5hmC) per site with modkit (see the
    module docstring). ``context``, ``hydroxymethyl`` and ``combine_strands``
    default to config methylation_context, methylation_5hmc and
    methylation_combine_strands, else CpG, 5mC only and strands combined."""

    COMMAND = "methylation pileup"

    input: Path = input_param(
        "Coordinate-sorted, indexed BAM or CRAM with MM/ML tags.",
        required_message="--input (a BAM or CRAM file) is required.",
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    region: str | None = param(
        None,
        help="Region(s), 1-based and inclusive: chr20:10000000-11000000 or "
        "chr1,chr2 (default: the whole genome)",
        flags=("-r", "--region"),
    )
    context: str | None = param(
        None,
        help="cpg: CpG sites only; all: every cytosine (CHG/CHH too). Overrides "
        "methylation_context in config.toml (default: cpg).",
        choices=("cpg", "all"),
    )
    hydroxymethyl: bool | None = param(
        None,
        help="Also report 5-hydroxymethylcytosine (Nanopore 5mCG_5hmCG models). "
        "Overrides methylation_5hmc in config.toml (default: 5mC only).",
        flags=("--5hmc",),
        action="boolean_optional",
    )
    combine_strands: bool | None = param(
        None,
        help="Sum both strands of each CpG into one row on its C. Overrides "
        "methylation_combine_strands in config.toml (default: on for CpG).",
        doc="Sum both strands of each CpG into one row on its C. True with "
        "context 'all' is an error; the config default quietly gives way.",
        action="boolean_optional",
    )
    table_format: str = param(
        "tsv",
        help="Summary table format: tsv, or parquet (needs pyarrow), which also "
        "writes the sites as <stem>.<context>.bedmethyl.parquet (default: tsv)",
        flags=("--format",),
        choices=FORMATS,
        cli_type=table_format_type(),
    )

    def write_folders(self) -> Iterator[Path]:
        # An unindexed reference FASTA is indexed beside itself.
        ref = self.ref.expanduser() if self.ref else None
        if ref is not None and not ref.is_dir():
            yield ref.absolute().parent


def pileup(
    params: MethylationPileupParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Tabulate base modifications (see ``MethylationPileupParams``)."""
    from wgsextract_cli.commands.methylation import run_pileup

    return execute(params, run_pileup, progress=progress, cancel=cancel, check=check)


__all__ = ["MethylationPileupParams", "pileup"]
