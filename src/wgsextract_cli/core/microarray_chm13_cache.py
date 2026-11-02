"""Content-verified derived CHM13 files in the reference library."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path

from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.microarray_aadr import _MARKERS
from wgsextract_cli.core.microarray_chm13 import CHM13Catalogue
from wgsextract_cli.core.microarray_targets import TARGET_VERSION, _template_paths
from wgsextract_cli.core.microarray_utils import (
    _PACKAGED_TEMPLATES,
    _resolve_templates_roots,
)

_VERSION = 1
MarkerIDs = dict[tuple[str, int], str]


def _key(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def cache_directory(
    library: str | None, catalogue: CHM13Catalogue, roots: list[str]
) -> Path | None:
    if not library or not catalogue.cache_key:
        return None
    files = [
        Path(path)
        for _name, _ncbi36, paths in _template_paths(_resolve_templates_roots(roots))
        for path in paths
    ]
    files.extend([_MARKERS, _PACKAGED_TEMPLATES / "corrections.tsv"])
    chain = next(
        (
            Path(root, "hg18ToHg19.over.chain.gz")
            for root in roots
            if Path(root, "hg18ToHg19.over.chain.gz").is_file()
        ),
        None,
    )
    if chain is not None:
        files.append(chain)
    fingerprints = []
    for path in files:
        stat = path.stat()
        fingerprints.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
    key = _key((_VERSION, TARGET_VERSION, catalogue.cache_key, fingerprints))
    return Path(library, "microarray", "chm13", key)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _record(path: Path) -> dict[str, int | str]:
    return {"size": path.stat().st_size, "sha256": _sha256(path)}


def _verified(path: Path) -> bool:
    try:
        record = json.loads(path.with_suffix(path.suffix + ".json").read_text())
        return bool(record == _record(path))
    except (OSError, ValueError):
        return False


def _publish(source: Path, destination: Path) -> None:
    record = _record(source)
    metadata = source.with_suffix(source.suffix + ".json")
    metadata.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    os.replace(source, destination)
    os.replace(metadata, destination.with_suffix(destination.suffix + ".json"))


def _target_record(path: Path) -> dict[str, dict[str, int | str]]:
    return {suffix: _record(Path(str(path) + suffix)) for suffix in ("", ".tbi")}


def _target_current(path: Path) -> bool:
    try:
        record = json.loads(Path(str(path) + ".json").read_text())
        return bool(record == _target_record(path))
    except (OSError, ValueError):
        return False


def _publish_target(source: Path, destination: Path) -> None:
    metadata = Path(str(source) + ".json")
    metadata.write_text(
        json.dumps(_target_record(source)), encoding="utf-8", newline="\n"
    )
    os.replace(source, destination)
    os.replace(str(source) + ".tbi", str(destination) + ".tbi")
    # Commit the pair only after both files are in place.
    os.replace(metadata, str(destination) + ".json")


def marker_ids(directory: Path | None, build: Callable[[], MarkerIDs]) -> MarkerIDs:
    if directory is None:
        return build()
    path = directory / "markers.tsv"

    def read() -> MarkerIDs | None:
        if not _verified(path):
            return None
        try:
            result = {}
            with path.open(encoding="utf-8") as source:
                for line in source:
                    chrom, position, marker = line.rstrip("\n").split("\t")
                    result[chrom, int(position)] = marker
            return result
        except (OSError, ValueError):
            return None

    cached = read()
    if cached is not None:
        return cached
    with ExitStack() as stack:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stack.enter_context(build_lock(str(path) + ".lock"))
        except OSError as error:
            logging.warning("CHM13 marker cache unavailable: %s", error)
            return build()
        cached = read()
        if cached is not None:
            return cached
        result = build()
        try:
            with tempfile.TemporaryDirectory(dir=directory, prefix="markers-") as work:
                staged = Path(work, path.name)
                with staged.open("w", encoding="utf-8", newline="\n") as destination:
                    for (chrom, position), marker in result.items():
                        destination.write(f"{chrom}\t{position}\t{marker}\n")
                _publish(staged, path)
        except OSError as error:
            logging.warning("Could not save CHM13 marker cache: %s", error)
        return result


def target(
    directory: Path | None,
    names: Mapping[str, str],
    regions: list[tuple[str, int, int]],
    build: Callable[[Path], str],
    work: Path,
) -> str:
    if directory is None:
        return build(work)
    path = directory / f"target-{_key((dict(names), regions))}.tab.gz"
    if _target_current(path):
        return str(path)
    with ExitStack() as stack:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stack.enter_context(build_lock(str(path) + ".lock"))
        except OSError as error:
            logging.warning("CHM13 target cache unavailable: %s", error)
            return build(work)
        if not _target_current(path):
            built = Path(build(work))
            try:
                with tempfile.TemporaryDirectory(
                    dir=directory, prefix="target-"
                ) as stage:
                    staged = Path(stage, path.name)
                    shutil.copyfile(built, staged)
                    shutil.copyfile(str(built) + ".tbi", str(staged) + ".tbi")
                    _publish_target(staged, path)
            except OSError as error:
                logging.warning("Could not save CHM13 target cache: %s", error)
                return str(built)
    return str(path)
