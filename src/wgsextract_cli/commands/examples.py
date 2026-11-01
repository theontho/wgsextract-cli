import argparse
import logging
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path
from urllib.parse import urlparse

from wgsextract_cli.core.download_progress import curl_progress_args, redact_urls
from wgsextract_cli.core.genome_library import GENOME_CONFIG_NAME
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)

from ._examples_catalog import (
    ASPERA_MAX_BANDWIDTH,
    ASPERA_PORT,
    ASPERA_RESUME_PARTIAL,
    ASPERA_ROOT,
    COLLECTION_DIR,
    CONFIG_ROLES,
    HTTPS_ROOT,
    GenomeExample,
    _resolve_aspera_key,
    _select_examples,
    _target_root,
    cmd_list,
)


def _resolve_method(method: str, aspera_key: str | None = None) -> str:
    if method in {"auto", "https"}:
        if shutil.which("curl") is None:
            raise WGSExtractError(
                "HTTPS downloads require 'curl' to be installed. Install curl "
                "or use --method aspera."
            )
        return "https"
    if method == "aspera":
        if shutil.which("ascp") is None:
            raise WGSExtractError(
                "Aspera requested but 'ascp' is not installed. Use --method https."
            )
        if not _resolve_aspera_key(aspera_key):
            raise WGSExtractError(
                "Aspera requested but no private key was found. Pass --aspera-key "
                "or use --method https."
            )
        return "aspera"
    raise WGSExtractError(f"Unsupported transfer method: {method}")


def _source_for(url_path: str, method: str) -> str:
    if url_path.startswith(("https://", "http://", "ftp://")):
        return url_path
    if method == "aspera":
        return f"{ASPERA_ROOT}/{url_path}"
    return f"{HTTPS_ROOT}/{url_path}"


def _filename(url_path: str) -> str:
    parsed = urlparse(url_path)
    name = Path(parsed.path if parsed.scheme else url_path).name
    if not name:
        raise WGSExtractError(
            f"URL path must end with a filename: {redact_urls(url_path)}"
        )
    return name


def _planned_downloads(
    example: GenomeExample, example_dir: Path, method: str
) -> list[tuple[str, Path, str, str]]:
    return [
        (
            _source_for(file.url_path, file.transfer_method or method),
            example_dir / _filename(file.url_path),
            file.role,
            file.transfer_method or method,
        )
        for file in example.files
    ]


def _partial_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".partial")


def _download_file(
    source: str,
    destination: Path,
    method: str,
    aspera_key: str | None = None,
    *,
    force: bool = False,
) -> None:
    """Download into <destination>.partial (resuming it if present), then rename."""
    partial = _partial_path(destination)
    logging.info(
        "%s %s", "Resuming" if partial.exists() else "Downloading", redact_urls(source)
    )
    if method != "aspera" and urlparse(source).scheme in {"http", "https"}:
        if not download_file(source, str(destination), use_cache=not force):
            raise WGSExtractError(f"Download failed. Re-run to resume from {partial}.")
        return
    try:
        if method == "aspera":
            segments = Path(str(partial) + ".segments")
            if segments.exists():
                partial.unlink(missing_ok=True)
                segments.unlink()
            Path(str(partial) + ".source").unlink(missing_ok=True)
            key = _resolve_aspera_key(aspera_key)
            if key is None:
                raise WGSExtractError("No Aspera private key available.")
            run_command(
                [
                    "ascp",
                    "-i",
                    str(key),
                    "-k",
                    ASPERA_RESUME_PARTIAL,
                    # Disable encryption (-T) for compatibility with EBI's
                    # public endpoint.
                    "-T",
                    "-P",
                    ASPERA_PORT,
                    "-l",
                    ASPERA_MAX_BANDWIDTH,
                    source,
                    str(partial),
                ],
                redact_output_urls=True,
            )
        else:
            run_command(
                [
                    "curl",
                    "--fail",
                    "--location",
                    *curl_progress_args(),
                    "--continue-at",
                    "-",
                    "--output",
                    str(partial),
                    source,
                ],
                redact_output_urls=True,
            )
        partial.replace(destination)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(
            f"Download failed for {redact_urls(source)}: "
            f"{redact_urls(str(e), sources=(source,))}. "
            f"Re-run to resume from {partial}."
        ) from e


def _write_genome_config(example: GenomeExample, example_dir: Path) -> None:
    """Write only primary input roles defined in CONFIG_ROLES."""
    lines = [
        "# WGS Extract per-genome configuration",
        "# Downloaded from the 1000 Genomes Project example catalog.",
        f"# Example: {example.example_id} ({example.label})",
        "",
    ]
    for file in example.files:
        if file.role in CONFIG_ROLES:
            lines.append(f'{file.role} = "{_filename(file.url_path)}"')
    (example_dir / GENOME_CONFIG_NAME).write_text("\n".join(lines) + "\n", newline="\n")


def _print_plan(
    example: GenomeExample, planned: list[tuple[str, Path, str, str]]
) -> None:
    print(f"{example.example_id}: {example.label}")
    for source, destination, role, _transfer_method in planned:
        print(f"  {role}: {redact_urls(source)}")
        print(f"        -> {destination}")


def cmd_download(args: Namespace) -> None:
    """Download selected example genomes and generate genome config files."""
    selected = _select_examples(args.example_ids, args.all, getattr(args, "tag", None))
    aspera_key = args.aspera_key
    method = _resolve_method(args.method, aspera_key)
    root = _target_root(args.target_root)
    collection_dir = root / COLLECTION_DIR

    logging.info(f"Downloading examples into {collection_dir}")
    for example in selected:
        example_dir = collection_dir / example.example_id
        planned = _planned_downloads(example, example_dir, method)
        if args.dry_run:
            _print_plan(example, planned)
            continue

        example_dir.mkdir(parents=True, exist_ok=True)
        for source, destination, _role, transfer_method in planned:
            if args.force:
                destination.unlink(missing_ok=True)
                _partial_path(destination).unlink(missing_ok=True)
                Path(str(_partial_path(destination)) + ".segments").unlink(
                    missing_ok=True
                )
                Path(str(_partial_path(destination)) + ".source").unlink(
                    missing_ok=True
                )
            elif destination.exists():
                logging.info(f"Skipping existing {destination}")
                continue
            _download_file(
                source, destination, transfer_method, aspera_key, force=args.force
            )
        _write_genome_config(example, example_dir)
        logging.info(
            f"Installed {example.label}. Use --genome "
            f"{COLLECTION_DIR}/{example.example_id}"
        )


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    """Register the example-genome list/download subcommands."""
    parser = subparsers.add_parser(
        "example-genome",
        help="Download curated 1000 Genomes example datasets.",
    )
    examples_subs = parser.add_subparsers(dest="examples_cmd", required=True)

    list_parser = examples_subs.add_parser(
        "list",
        parents=[base_parser],
        help="List curated 1000 Genomes examples.",
    )
    list_parser.add_argument(
        "--target-root",
        help=(
            "Genome library root. Defaults to config genome_library, or genomes/ "
            "in the source checkout (or user data directory) when unset."
        ),
    )
    list_parser.add_argument(
        "--tag",
        action="append",
        help="Only list examples with this tag. May be repeated (e.g. --tag pacbio).",
    )
    list_parser.set_defaults(func=cmd_list)

    download_parser = examples_subs.add_parser(
        "download",
        parents=[base_parser],
        help="Download curated 1000 Genomes examples into the genome library.",
    )
    download_parser.add_argument(
        "example_ids",
        nargs="*",
        metavar="EXAMPLE_ID",
        help="Example IDs to download. Defaults to a small starter set.",
    )
    download_parser.add_argument(
        "--tag",
        action="append",
        help="Download every example with this tag. May be repeated (e.g. --tag pacbio).",
    )
    download_parser.add_argument(
        "--all",
        action="store_true",
        help="Download every curated example, including large full-genome data.",
    )
    download_parser.add_argument(
        "--method",
        choices=("auto", "https", "aspera"),
        default="auto",
        help=(
            "Transfer method. Auto is an alias for HTTPS and does not probe "
            "Aspera; use --method aspera explicitly for Aspera."
        ),
    )
    download_parser.add_argument(
        "--aspera-key",
        help="Private key for Aspera ascp downloads. Required with --method aspera.",
    )
    download_parser.add_argument(
        "--target-root",
        help=(
            "Genome library root. Defaults to config genome_library, or genomes/ "
            "in the source checkout (or user data directory) when unset."
        ),
    )
    download_parser.add_argument(
        "--force",
        action="store_true",
        help="Discard existing and partially downloaded files and download again. "
        "Without it, completed files are kept and interrupted downloads resume.",
    )
    download_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned downloads without creating files.",
    )
    download_parser.set_defaults(func=cmd_download)
