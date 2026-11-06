"""``example-genome``: the curated 1000 Genomes example datasets.

``list_examples`` describes the catalog; its result's ``details`` hold the
collection folder and each example. ``download`` fetches the chosen examples
into ``<target_root>/test-1000genomes/<example id>`` and writes each one's
``genome-config.toml``, so ``--genome test-1000genomes/<example id>`` finds
them. Completed files are kept and interrupted downloads resume unless
``force`` is set. A ``dry_run`` downloads nothing; its result's ``details``
hold the plan."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute

METHODS = ("auto", "https", "aspera")


def _target_root_param() -> Any:
    return param(
        None,
        help=(
            "Genome library root. Defaults to config genome_library, or genomes/ "
            "in the source checkout (or user data directory) when unset."
        ),
        doc="Genome library folder holding the test-1000genomes collection "
        "(default: config genome_library, else the genomes folder of the "
        "checkout or user data folder).",
        path="directory",
    )


@dataclass(frozen=True, kw_only=True)
class ExampleGenomeListParams(Params):
    """List the curated examples: each one's id, label, data type, size,
    tags and file count."""

    COMMAND = "example-genome list"

    target_root: Path | None = _target_root_param()
    tag: tuple[str, ...] | None = param(
        None,
        help="Only list examples with this tag. May be repeated (e.g. --tag pacbio).",
        doc="Only examples carrying every one of these tags.",
        action="append",
    )


@dataclass(frozen=True, kw_only=True)
class ExampleGenomeDownloadParams(Params):
    """Download curated examples into the genome library and write their
    genome configs. With no ``example_ids``, ``tag`` or ``all``, the small
    starter set (a chrMT VCF and a chr20 BAM) is downloaded."""

    COMMAND = "example-genome download"

    example_ids: tuple[str, ...] = param(
        (),
        help="Example IDs to download. Defaults to a small starter set.",
        doc="Example ids, as list_examples gives them; not with tag or all.",
        metavar="EXAMPLE_ID",
        positional=True,
    )
    tag: tuple[str, ...] | None = param(
        None,
        help="Download every example with this tag. May be repeated "
        "(e.g. --tag pacbio).",
        doc="Download every example carrying all of these tags.",
        action="append",
    )
    all: bool = param(
        False,
        help="Download every curated example, including large full-genome data.",
        action="store_true",
    )
    method: str = param(
        "auto",
        help=(
            "Transfer method. Auto is an alias for HTTPS and does not probe "
            "Aspera; use --method aspera explicitly for Aspera."
        ),
        choices=METHODS,
    )
    aspera_key: Path | None = param(
        None,
        help="Private key for Aspera ascp downloads. Required with --method aspera.",
        doc="Private key for Aspera downloads (default: the Aspera Connect key, "
        "if installed).",
        path="file",
    )
    target_root: Path | None = _target_root_param()
    force: bool = param(
        False,
        help="Discard existing and partially downloaded files and download again. "
        "Without it, completed files are kept and interrupted downloads resume.",
        action="store_true",
    )
    dry_run: bool = param(
        False,
        help="Print the planned downloads without creating files.",
        doc="Plan the downloads (the result's details) without creating files.",
        action="store_true",
    )

    def write_folders(self) -> Iterator[Path]:
        from wgsextract_cli.commands._examples_catalog import (
            COLLECTION_DIR,
            _target_root,
        )

        yield _target_root(self.target_root) / COLLECTION_DIR


def list_examples(
    params: ExampleGenomeListParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Describe the example catalog; the description is the result's
    ``details``."""
    from wgsextract_cli.commands.examples import run_list

    return execute(params, run_list, progress=progress, cancel=cancel, check=check)


def download(
    params: ExampleGenomeDownloadParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download example genomes (see ``ExampleGenomeDownloadParams``); each file
    is an output with its catalog role (``bam``, ``vcf``, ``fastq_r1`` ...),
    plus each example's ``genome_config``."""
    from wgsextract_cli.commands.examples import run_download

    return execute(params, run_download, progress=progress, cancel=cancel, check=check)


__all__ = [
    "ExampleGenomeDownloadParams",
    "ExampleGenomeListParams",
    "download",
    "list_examples",
]
