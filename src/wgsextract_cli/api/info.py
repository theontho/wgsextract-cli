"""``info``: what a BAM/CRAM is (build, sequencer, sex, depth) and its coverage;
``info chip``: which microarray chip a raw-data file came from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    input_param,
    optional_input_param,
    outdir_param,
    ref_param,
    table_format_param,
)

_ALIGNMENT = "BAM or CRAM to describe."
_TABLES = (
    "Also save the per-chromosome table and the QC stats as "
    "<input>_chromosomes and <input>_qc .tsv or .parquet (needs pyarrow); "
    "implies --detailed"
)


@dataclass(frozen=True, kw_only=True)
class InfoParams(Params):
    """Identify the reference build, file integrity and sequencing metrics of a
    BAM/CRAM. Metrics are cached next to the outputs as
    ``<input>.wgse_info.json``."""

    COMMAND = "info"

    input: Path = input_param(_ALIGNMENT, "--input required.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    # Detailed metrics save the samtools coverage table.
    detailed: bool = param(
        False, help=CLI_HELP["arg_detailed"], action="store_true", writes=True
    )
    table_format: str | None = table_format_param(_TABLES)
    cache: bool = param(
        True,
        help="Save the metrics as <input>.wgse_info.json for later runs (default: on)",
        doc="Save the metrics as ``<input>.wgse_info.json`` for later runs; an "
        "existing cache is read either way.",
        action="boolean_optional",
        safe_value=False,
    )


@dataclass(frozen=True, kw_only=True)
class CalculateCoverageParams(Params):
    """Exact per-chromosome breadth and depth of coverage (``samtools coverage``)."""

    COMMAND = "info calculate-coverage"

    input: Path = input_param(_ALIGNMENT, "--input required.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    region: str | None = param(
        None, help=CLI_HELP["arg_region"], flags=("-r", "--region")
    )
    bins: bool = param(
        False,
        help="Report exact per-contig depth 0, 1-3, 4-7 and 8+ breadth in one "
        "streaming pass.",
        action="store_true",
    )
    by: Path | None = param(
        None,
        help="Report depth per target of this BED (e.g. from 'panel bed'): mean, "
        "median and the fraction at >=10x, 20x, 30x and --min-depth, plus the "
        "on- versus off-target enrichment",
        flags=("--by",),
        path="file",
        metavar="BED",
    )
    min_depth: int = param(
        30,
        help="With --by, list targets whose mean depth is below this (default: 30)",
        flags=("--min-depth",),
        metavar="N",
        minimum=0,
    )
    table_format: str | None = table_format_param(
        f"{_TABLES}. With --bins or --by, that table's format instead",
        doc=f"{_TABLES}; None saves none. With bins or by, that table's "
        "format instead (None: tsv).",
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.by is not None and (self.bins or self.region):
            from wgsextract_cli.core.utils import UsageError

            raise UsageError("--by can't be combined with --bins or --region.")


@dataclass(frozen=True, kw_only=True)
class CoverageSampleParams(Params):
    """Estimated per-chromosome breadth of coverage from ~1,000 sampled windows."""

    COMMAND = "info coverage-sample"

    input: Path = input_param(_ALIGNMENT, "--input required.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    region: str | None = param(
        None, help=CLI_HELP["arg_region"], flags=("-r", "--region")
    )
    table_format: str | None = table_format_param(_TABLES)


@dataclass(frozen=True, kw_only=True)
class ChipParams(Params):
    """Identify the vendor, chip and revision of a consumer microarray
    raw-data file, or list the catalogued revisions."""

    COMMAND = "info chip"

    input: Path | None = optional_input_param(
        "Microarray raw-data file; required unless list_revisions."
    )
    ref: Path | None = ref_param()
    list_revisions: bool = param(
        False,
        help="List the catalogued chip revisions and their matching templates.",
        flags=("--list",),
        dest="list",
        action="store_true",
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.input is None and not self.list_revisions:
            from wgsextract_cli.core.utils import UsageError

            raise UsageError("--input required (a microarray raw-data file).")


def info(
    params: InfoParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Describe a BAM/CRAM.

    ``details`` holds the metrics (``ref_model_str``, ``gender``,
    ``sequencer``, ``avg_read_len``, ``metrics``, ...); ``metrics`` holds the
    headline numbers.
    """
    from wgsextract_cli.commands.info.runner import run_info

    return execute(params, run_info, progress=progress, cancel=cancel, check=check)


def calculate_coverage(
    params: CalculateCoverageParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Exact coverage; outputs the coverage table (or the depth-bins TSV)."""
    from wgsextract_cli.commands.info.runner import run_info

    return execute(params, run_info, progress=progress, cancel=cancel, check=check)


def coverage_sample(
    params: CoverageSampleParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Sampled coverage estimate; outputs the coverage table."""
    from wgsextract_cli.commands.info.runner import run_info

    return execute(params, run_info, progress=progress, cancel=cancel, check=check)


def chip(
    params: ChipParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Identify a raw-data file's chip; ``details`` holds the candidates and
    ``metrics`` the best ``revision``, ``match`` and ``confidence``."""
    from wgsextract_cli.commands.info.chip import run_chip

    return execute(params, run_chip, progress=progress, cancel=cancel, check=check)
