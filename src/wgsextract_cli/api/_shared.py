"""Fields every command shares with the CLI's global options."""

from __future__ import annotations

from typing import Any

from wgsextract_cli.core.resource_policy import parse_thread_count
from wgsextract_cli.core.steps import (
    DRY_RUN_HELP,
    RESTART_HELP,
    RESUME_GROUP,
    force_step_help,
)
from wgsextract_cli.core.table_writer import FORMATS, table_format_type

from ._params import param


def input_param(doc: str, required_message: str = "--input is required.") -> Any:
    return param(
        doc=doc,
        flags=("--input", "-i"),
        path="file",
        shared=True,
        required_message=required_message,
    )


def optional_input_param(doc: str) -> Any:
    return param(None, doc=doc, flags=("--input", "-i"), path="file", shared=True)


def ref_param(*, local_only: bool = False) -> Any:
    return param(
        None,
        doc="Reference FASTA or reference library folder; resolved from the "
        "input's header when omitted (default: config reference_fasta).",
        flags=("--ref",),
        path="file_or_directory",
        shared=True,
        config_key="reference_fasta",
        local_only=local_only,
    )


def outdir_param(default_text: str = "the input's folder") -> Any:
    return param(
        None,
        doc=f"Folder for the outputs (default: config output_directory, "
        f"else {default_text}).",
        flags=("--outdir", "-o"),
        path="directory",
        shared=True,
        config_key="output_directory",
        writes=True,
    )


def threads_param() -> Any:
    return param(
        None,
        doc="CPU threads; None picks a count for this machine "
        "(default: config cpu_threads, else auto).",
        flags=("--threads", "-t"),
        shared=True,
        cli_type=parse_thread_count,
        config_key="cpu_threads",
    )


def memory_param() -> Any:
    return param(
        None,
        doc="Memory limit per thread, e.g. '2G'; None picks a share of "
        "this machine's RAM (default: config memory_limit, else auto).",
        flags=("--memory", "-m"),
        shared=True,
        config_key="memory_limit",
    )


def depth_param() -> Any:
    """``--qc-min-depth``, as ``genotype_qc.add_depth_argument`` declares it."""
    from wgsextract_cli.core.genotype_qc import DEPTH_HELP, _non_negative

    return param(
        None, help=DEPTH_HELP, cli_type=_non_negative, metavar="DEPTH", minimum=0
    )


def table_format_param(help: str, *, hint: str = "", doc: str | None = None) -> Any:
    """``--format``: also save a table as "tsv" or "parquet" (needs pyarrow);
    ``None`` saves none. ``hint`` is added to the error for another value."""
    return param(
        None,
        help=help,
        doc=doc or f"{help}. None saves no table.",
        flags=("--format",),
        choices=FORMATS,
        cli_type=table_format_type(hint),
        writes=True,
    )


def restart_param() -> Any:
    """``--force``/``--no-resume`` of a step workflow (``core.steps``)."""
    return param(
        False,
        help=RESTART_HELP,
        flags=("--force", "--no-resume"),
        action="store_true",
        group=RESUME_GROUP,
    )


def force_steps_param(steps: tuple[str, ...]) -> Any:
    return param(
        None,
        help=force_step_help(steps),
        doc="Steps to rerun with the steps that use their outputs; None reruns "
        f"none. Steps: {', '.join(steps)}.",
        flags=("--force-step",),
        metavar="STEP",
        choices=steps,
        action="append",
        group=RESUME_GROUP,
    )


def dry_run_param() -> Any:
    return param(
        False,
        help=DRY_RUN_HELP,
        doc="Log which steps would run or be skipped, and why, and run none.",
        action="store_true",
        group=RESUME_GROUP,
    )


_CONFIDENCE_DOC = (
    "(LOW, HIGH): {what} below LOW labels a call low confidence, below HIGH "
    "moderate (default: config confidence_min_* and confidence_high_*)."
)


def confidence_depth_param() -> Any:
    from wgsextract_cli.core.variant_confidence import DEPTH_HELP

    return _confidence_param(DEPTH_HELP, "read depth")


def confidence_gq_param() -> Any:
    from wgsextract_cli.core.variant_confidence import GQ_HELP

    return _confidence_param(GQ_HELP, "genotype quality")


def _confidence_param(help: str, what: str) -> Any:
    from wgsextract_cli.core.variant_confidence import _non_negative

    return param(
        None,
        help=help,
        cli_type=_non_negative,
        minimum=0,
        metavar=("LOW", "HIGH"),
        doc=_CONFIDENCE_DOC.format(what=what),
    )


def check_confidence(params: Any) -> None:
    """Refuse crossed confidence thresholds (LOW above HIGH) before any work."""
    from wgsextract_cli.core.variant_confidence import thresholds_from

    thresholds_from(params)


def no_gpu_memory_check_param() -> Any:
    return param(
        False,
        help="With --gpu, run Parabricks even when the GPU or host memory is "
        "below what it needs (it may fail partway).",
        doc="Run Parabricks despite too little GPU or host memory; the "
        "low-memory options are still chosen to fit.",
        action="store_true",
    )
