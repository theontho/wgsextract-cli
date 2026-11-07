import argparse
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import tomli_w

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api._runtime import cancel_event, checkpoint
from wgsextract_cli.api.examples import (
    ExampleGenomeDownloadParams,
    ExampleGenomeListParams,
)
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.download_progress import curl_progress_args, redact_urls
from wgsextract_cli.core.genome_library import GENOME_CONFIG_NAME
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.results import current_result, human_output
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
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
    EXAMPLES,
    HTTPS_ROOT,
    GenomeExample,
    _filter_examples_by_tags,
    _resolve_aspera_key,
    _select_examples,
    _target_root,
)


def _resolve_method(method: str, aspera_key: str | None = None) -> str:
    if method in {"auto", "https"}:
        if shutil.which("curl") is None:
            raise MissingDependencyError(
                "HTTPS downloads require 'curl' to be installed. Install curl "
                "or use --method aspera."
            )
        return "https"
    if method == "aspera":
        if shutil.which("ascp") is None:
            raise MissingDependencyError(
                "Aspera requested but 'ascp' is not installed. Use --method https."
            )
        if not _resolve_aspera_key(aspera_key):
            raise UsageError(
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


def _download_paths(destination: Path) -> list[Path]:
    """A download's file and the sidecars resuming it."""
    partial = _partial_path(destination)
    return [
        destination,
        partial,
        Path(str(partial) + ".segments"),
        Path(str(partial) + ".source"),
    ]


def written_paths(
    params: ExampleGenomeDownloadParams, collection: Path
) -> dict[Path, list[Path]]:
    """Each selected example's folder in ``collection`` and the files a
    download writes in it; empty when the selection is invalid (the download
    fails before writing)."""
    try:
        selected = _select_examples(params.example_ids, params.all, params.tag)
    except WGSExtractError:
        return {}
    written = {}
    for example in selected:
        folder = collection / example.example_id
        written[folder] = [
            *(
                path
                for file in example.files
                for path in _download_paths(folder / _filename(file.url_path))
            ),
            folder / GENOME_CONFIG_NAME,
        ]
    return written


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
        if not download_file(
            source, str(destination), cancel_event=cancel_event(), use_cache=not force
        ):
            checkpoint()  # a cancelled download is not a failed one
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
) -> dict[str, object]:
    human_output(f"{example.example_id}: {example.label}")
    files = []
    for source, destination, role, _transfer_method in planned:
        human_output(f"  {role}: {redact_urls(source)}")
        human_output(f"        -> {destination}")
        files.append(
            {"role": role, "source": redact_urls(source), "path": str(destination)}
        )
    return {"id": example.example_id, "label": example.label, "files": files}


def run_list(params: ExampleGenomeListParams) -> None:
    """List available example genomes with metadata and usage instructions."""
    collection = _target_root(params.target_root) / COLLECTION_DIR
    listed = []
    human_output(f"Target collection: {collection}")
    human_output()
    for example in _filter_examples_by_tags(EXAMPLES, params.tag):
        use = f"wgsextract --genome {COLLECTION_DIR}/{example.example_id} ..."
        human_output(f"{example.example_id}")
        human_output(f"  label: {example.label}")
        human_output(f"  type:  {example.data_type}")
        human_output(f"  size:  {example.size}")
        if example.tags:
            human_output(f"  tags:  {', '.join(example.tags)}")
        human_output(f"  files: {len(example.files)}")
        human_output(f"  use:   {use}")
        human_output()
        listed.append(
            {
                "id": example.example_id,
                "label": example.label,
                "type": example.data_type,
                "size": example.size,
                "tags": list(example.tags),
                "files": len(example.files),
                "use": use,
            }
        )
    current_result().set_details({"collection": str(collection), "examples": listed})


def run_download(params: ExampleGenomeDownloadParams) -> None:
    """Download selected example genomes and generate genome config files."""
    selected = _select_examples(params.example_ids, params.all, params.tag)
    aspera_key = os.fspath(params.aspera_key) if params.aspera_key else None
    method = _resolve_method(params.method, aspera_key)
    root = _target_root(params.target_root)
    collection_dir = root / COLLECTION_DIR

    logging.info(f"Downloading examples into {collection_dir}")
    plan = []
    for example in selected:
        example_dir = collection_dir / example.example_id
        planned = _planned_downloads(example, example_dir, method)
        if params.dry_run:
            plan.append(_print_plan(example, planned))
            continue

        example_dir.mkdir(parents=True, exist_ok=True)
        for source, destination, role, transfer_method in planned:
            checkpoint()
            if params.force:
                for path in _download_paths(destination):
                    path.unlink(missing_ok=True)
            elif destination.exists():
                logging.info(f"Skipping existing {destination}")
                current_result().add_output(destination, role)
                continue
            _download_file(
                source, destination, transfer_method, aspera_key, force=params.force
            )
            current_result().add_output(destination, role)
        _write_genome_config(example, example_dir)
        current_result().add_output(example_dir / GENOME_CONFIG_NAME, "genome_config")
        library = settings.get("genome_library")
        hint = (
            ""
            if library and Path(library).expanduser().resolve() == root
            else " after setting "
            + tomli_w.dumps({"genome_library": str(root)}).strip()
            + " in config.toml"
        )
        logging.info(
            f"Installed {example.label}. Use --genome "
            f"{COLLECTION_DIR}/{example.example_id}{hint}"
        )
    if params.dry_run:
        current_result().set_details(
            {"collection": str(collection_dir), "examples": plan}
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
    for name, cls, impl, help_text in (
        ("list", ExampleGenomeListParams, run_list,
         "List curated 1000 Genomes examples."),
        ("download", ExampleGenomeDownloadParams, run_download,
         "Download curated 1000 Genomes examples into the genome library."),
    ):  # fmt: skip
        sub = examples_subs.add_parser(name, parents=[base_parser], help=help_text)
        add_arguments(sub, cls)
        sub.set_defaults(func=cli_handler(cls, impl))
