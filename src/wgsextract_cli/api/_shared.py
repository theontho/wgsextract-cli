"""Fields every command shares with the CLI's global options."""

from __future__ import annotations

from typing import Any

from wgsextract_cli.core.resource_policy import parse_thread_count
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


def ref_param() -> Any:
    return param(
        None,
        doc="Reference FASTA or reference library folder; resolved from the "
        "input's header when omitted (default: config reference_fasta).",
        flags=("--ref",),
        path="file_or_directory",
        shared=True,
        config_key="reference_fasta",
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


def depth_param() -> Any:
    """``--qc-min-depth``, as ``genotype_qc.add_depth_argument`` declares it."""
    from wgsextract_cli.core.genotype_qc import DEPTH_HELP, _non_negative

    return param(None, help=DEPTH_HELP, cli_type=_non_negative, metavar="DEPTH")


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
