"""Content-verified derived CHM13 files in the reference library."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
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


def _verified(path: Path) -> bool:
    try:
        record = json.loads(path.with_suffix(path.suffix + ".json").read_text())
        return bool(
            record
            == {
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    except (OSError, ValueError):
        return False


def _publish(source: Path, destination: Path) -> None:
    record = {"size": source.stat().st_size, "sha256": _sha256(source)}
    metadata = source.with_suffix(source.suffix + ".json")
    metadata.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    os.replace(source, destination)
    os.replace(metadata, destination.with_suffix(destination.suffix + ".json"))


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
    directory.mkdir(parents=True, exist_ok=True)
    with build_lock(str(path) + ".lock"):
        cached = read()
        if cached is not None:
            return cached
        result = build()
        with tempfile.TemporaryDirectory(dir=directory, prefix="markers-") as work:
            staged = Path(work, path.name)
            with staged.open("w", encoding="utf-8", newline="\n") as destination:
                for (chrom, position), marker in result.items():
                    destination.write(f"{chrom}\t{position}\t{marker}\n")
            _publish(staged, path)
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
    index = Path(str(path) + ".tbi")
    if _verified(path) and _verified(index):
        return str(path)
    directory.mkdir(parents=True, exist_ok=True)
    with build_lock(str(path) + ".lock"):
        if not (_verified(path) and _verified(index)):
            with tempfile.TemporaryDirectory(dir=directory, prefix="target-") as stage:
                built = Path(build(Path(stage)))
                _publish(built, path)
                _publish(Path(str(built) + ".tbi"), index)
    return str(path)
