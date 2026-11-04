"""``lineage``: mtDNA haplogroups (Haplogrep), Y haplogroups (native Yleaf)
and Y-STR repeat counts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.ydna.ystr.panel import PANELS

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import depth_param, input_param, outdir_param, ref_param

_REQUIRED = LOG_MESSAGES["input_required"]
BUILDS = ("hg19", "hg38", "t2t")
TREES = ("yfull", "yfull_v10", "ftdna", "isogg")


def _build_param() -> Any:
    return param(
        None,
        help="Override the detected reference build for this run.",
        choices=BUILDS,
    )


@dataclass(frozen=True, kw_only=True)
class MtHaplogroupParams(Params):
    """Predict the mtDNA haplogroup with Haplogrep from a BAM/CRAM (chrM is
    called first; needs the reference) or a VCF. The report is
    ``haplogrep_results.txt`` in the output folder."""

    COMMAND = "lineage mt-haplogroup"

    input: Path = input_param("BAM, CRAM or VCF with chrM/MT.", _REQUIRED)
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    haplogrep_path: str | None = param(
        None,
        help="Path to haplogrep executable or JAR (optional if in PATH)",
        config_key="haplogrep_executable",
    )
    qc_min_depth: float | None = depth_param()


@dataclass(frozen=True, kw_only=True)
class YHaplogroupParams(Params):
    """Predict the Y haplogroup with the bundled native Yleaf from a BAM, CRAM
    or single-sample VCF/BCF. Outputs go to ``yleaf/`` in the output folder."""

    COMMAND = "lineage y-haplogroup"

    input: Path = input_param("BAM, CRAM or single-sample VCF/BCF.", _REQUIRED)
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    pos_file: Path | None = param(
        None, help="Yleaf position file (optional)", path="file"
    )
    build: str | None = _build_param()
    tree: str = param(
        "yfull",
        help="Yleaf v4.1.6 haplogroup tree (default: yfull).",
        choices=TREES,
    )
    min_reads: int = param(
        10, doc="Reads needed at a marker position for it to be used."
    )
    base_quality: int = param(
        20, doc="Minimum base quality of a read at a marker position."
    )
    base_majority: int = param(
        90, doc="Percentage of reads at a marker that must agree on the base."
    )
    min_score: float = param(
        0.95, doc="Minimum QC score of a reported haplogroup prediction."
    )
    ancient: bool = param(
        False, help="Use ancient-DNA marker tables.", action="store_true"
    )
    no_ref_inference: bool = param(
        False,
        help="VCF: use genotyped positions only (targeted panels); otherwise --ref "
        "is required.",
        action="store_true",
    )
    qc_min_depth: float | None = depth_param()


@dataclass(frozen=True, kw_only=True)
class YStrParams(Params):
    """Call Y-STR repeat counts from a BAM/CRAM into ``ystr/`` (a table and a
    FamilyTreeDNA-order CSV)."""

    COMMAND = "lineage y-str"

    input: Path = input_param("Indexed BAM or CRAM.", _REQUIRED)
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    panel: str = param(
        "Y-111",
        help="Marker panel: FamilyTreeDNA Y-12 to Y-111, PowerPlex Y23, "
        "Yfiler Plus, or all (default: Y-111).",
        choices=PANELS,
    )
    build: str | None = _build_param()
    min_reads: int = param(
        2, help="Spanning reads needed to support a repeat count (default: 2)."
    )
    min_fraction: float = param(
        0.6, help="Share of spanning reads that must agree (default: 0.6)."
    )
    min_mapq: int = param(
        20, help="Minimum mapping quality for single-copy markers (default: 20)."
    )


def mt_haplogroup(
    params: MtHaplogroupParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """mtDNA haplogroup; ``metrics`` holds ``haplogroup`` and
    ``haplogroup_quality``, ``details["haplogrep"]`` the full Haplogrep row."""
    from wgsextract_cli.commands.lineage import run_mt_haplogroup

    return execute(
        params, run_mt_haplogroup, progress=progress, cancel=cancel, check=check
    )


def y_haplogroup(
    params: YHaplogroupParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Y haplogroup; ``metrics`` holds ``haplogroup``, ``haplogroup_marker`` and
    ``qc_score``, ``details["predictions"]`` every Yleaf prediction row."""
    from wgsextract_cli.commands._lineage_ydna import run_y_haplogroup

    return execute(
        params, run_y_haplogroup, progress=progress, cancel=cancel, check=check
    )


def y_str(
    params: YStrParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Y-STR calls; ``details["calls"]`` lists every marker with its value and
    read support."""
    from wgsextract_cli.commands._lineage_ystr import run_y_str

    return execute(params, run_y_str, progress=progress, cancel=cancel, check=check)
