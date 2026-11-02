# SPDX-License-Identifier: GPL-3.0-or-later
# Data names derived from genid/Yleaf v4.1.6, yleaf/yleaf_constants.py
# (commit recorded in provenance.json).
# Copyright Arwin Ralf, Diego Montiel Gonzalez, Kaiyin Zhong, Manfred Kayser
# and Bram van Wersch, Erasmus MC Department of Genetic Identification.
# Modified 2026-11-01 by WGS Extract contributors: pinned release cache.
"""Download only the selected build's tables and the prediction trees."""

import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import WGSExtractError


def manifest() -> dict[str, Any]:
    with (
        Path(__file__).with_name("data_manifest.json").open(encoding="utf-8") as stream
    ):
        result: dict[str, Any] = json.load(stream)
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(root: Path, files: dict[str, Any]) -> bool:
    for name, metadata in files.items():
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != metadata["size"]
            or file_sha256(path) != metadata["sha256"]
        ):
            return False
    return True


def _extract(archive: Path, root: Path, files: dict[str, Any]) -> None:
    """Extract only manifest-listed regular files; never follow archive paths."""
    with tarfile.open(archive, "r:gz") as stream:
        seen = set()
        for entry in stream:
            if (
                not entry.isfile()
                or entry.name not in files
                or entry.name in seen
                or Path(entry.name).is_absolute()
                or ".." in Path(entry.name).parts
                or entry.size != files[entry.name]["size"]
            ):
                raise WGSExtractError(
                    "Unsafe or unexpected file in Yleaf data archive."
                )
            seen.add(entry.name)
            source = stream.extractfile(entry)
            if source is None:
                raise WGSExtractError("Unreadable Yleaf data archive member.")
            target = root / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
        if seen != set(files):
            raise WGSExtractError("Incomplete Yleaf data archive.")
    if not _verified(root, files):
        raise WGSExtractError("Yleaf data failed extracted-file SHA-256 verification.")


def ensure_data(build: str, library: Path | None = None) -> Path:
    if build not in ("hg19", "hg38", "t2t"):
        raise WGSExtractError(f"Unsupported Yleaf build: {build}")
    metadata = manifest()
    root: Path = (
        (library or Path(resolve_reference_library()))
        / "yleaf"
        / str(metadata["upstream"]["tag"])
    )
    root.mkdir(parents=True, exist_ok=True)
    with build_lock(str(root / "data.lock")):
        for key in (build, "hg_prediction_tables"):
            asset = metadata["archives"][key]
            if _verified(root, asset["files"]):
                continue
            logging.info(
                "Downloading Yleaf %s tables (%s)...", key, metadata["upstream"]["tag"]
            )
            archive = root / Path(asset["url"]).name
            try:
                if not download_file(
                    asset["url"],
                    str(archive),
                    expected_sha256=asset["sha256"],
                    resolve_release_digest=False,
                ):
                    raise WGSExtractError(
                        f"Cannot download verified Yleaf {key} data. If offline, "
                        "connect once to populate the configured reference_library."
                    )
                with tempfile.TemporaryDirectory(prefix="unpack-", dir=root) as staging:
                    stage = Path(staging)
                    _extract(archive, stage, asset["files"])
                    # Individual atomic replaces are protected by the library lock.
                    for name in asset["files"]:
                        target = root / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        (stage / name).replace(target)
            except (OSError, tarfile.TarError) as error:
                raise WGSExtractError(
                    f"Unable to prepare Yleaf {key} data: {error}"
                ) from error
            finally:
                archive.unlink(missing_ok=True)
    return root
