# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned Yleaf haplogroup trees: which tree, version, date, license, source.

``trees.json`` describes each ``--tree``. Yleaf v4.1.6's data archives carry the
yfull_v10, ftdna and isogg trees and their marker tables. The yfull tree is a
separate pack built from YFull's YTree by ``scripts/build_yfull_tree.py``,
pinned by SHA-256 and verified into the reference library on first use.
"""

import json
import logging
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.dev_download_cache import process_umask
from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import WGSExtractError

from .data import ensure_data, extract_archive, verified
from .markers import position_file
from .predict import TREE_FILES

MANIFEST: dict[str, Any] = json.loads(
    Path(__file__).with_name("trees.json").read_text(encoding="utf-8")
)
TREE_IDS: tuple[str, ...] = tuple(MANIFEST["trees"])
DEFAULT_TREE: str = MANIFEST["default"]


@dataclass(frozen=True)
class YleafTree:
    id: str
    name: str
    version: str
    date: str
    license: str
    source: str
    tree_file: Path
    tables: Path
    """Yleaf's hg_prediction_tables (QC1 backbone tables)."""
    positions_root: Path
    pack: bool

    def positions(self, build: str, ancient: bool = False) -> Path:
        if self.pack:
            name = "new_positions_ancient.txt" if ancient else "new_positions.txt"
            return self.positions_root / build / name
        return position_file(self.positions_root, build, self.id, ancient)

    @property
    def label(self) -> str:
        return tree_label(self.provenance())

    def provenance(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "date": self.date,
            "license": self.license,
            "source": self.source,
        }


def tree_label(meta: dict[str, Any]) -> str:
    """E.g. ``YFull YTree 14.06.0 (2026-09-22, CC-BY-4.0)``."""
    notes = [meta["date"]] if meta["date"] != meta["version"] else []
    unknown = meta["license"] == "unspecified"
    notes.append("license unspecified" if unknown else meta["license"])
    return f"{meta['name']} {meta['version']} ({', '.join(notes)})"


def tree_metadata(tree_id: str) -> dict[str, Any]:
    meta: dict[str, Any] | None = MANIFEST["trees"].get(tree_id)
    if meta is None:
        raise WGSExtractError(
            f"Unknown Yleaf tree {tree_id!r}; choose one of {', '.join(TREE_IDS)}."
        )
    return meta


def ensure_pack(tree_id: str, library: Path | None = None) -> Path:
    """The pinned tree pack's folder, downloaded and verified if missing."""
    meta = tree_metadata(tree_id)
    version = str(meta["version"])
    base = Path(library or resolve_reference_library())
    root = base / "yleaf" / "trees" / tree_id / version
    files: dict[str, Any] = meta["files"]
    if verified(root, files):
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    with build_lock(str(root.parent / f"{version}.lock")):
        if verified(root, files):
            return root
        logging.info(f"Downloading Yleaf tree {meta['name']} {version}...")
        archive = root.parent / f"{tree_id}-{version}.tar.gz"
        try:
            if not download_file(
                meta["url"],
                str(archive),
                expected_sha256=meta["sha256"],
                resolve_release_digest=False,
            ):
                raise WGSExtractError(
                    f"Cannot download the verified Yleaf tree {meta['name']} "
                    f"{version}. If offline, connect once to populate the "
                    "reference library."
                )
            staging = Path(tempfile.mkdtemp(prefix="unpack-", dir=root.parent))
            try:
                extract_archive(archive, staging, files)
                # mkdtemp makes 0700; publish readable to a shared library's users.
                if os.name != "nt":
                    os.chmod(staging, 0o777 & ~process_umask())
                shutil.rmtree(root, ignore_errors=True)
                staging.replace(root)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        except (OSError, tarfile.TarError) as error:
            raise WGSExtractError(
                f"Unable to prepare Yleaf tree {meta['name']} {version}: {error}"
            ) from error
        finally:
            archive.unlink(missing_ok=True)
    return root


def ensure_tree(tree_id: str, build: str, library: Path | None = None) -> YleafTree:
    """The tree and the ``build`` marker tables it needs, downloaded if missing."""
    meta = tree_metadata(tree_id)
    pack = "url" in meta
    # A pack has its own marker tables; Yleaf's archive supplies QC1's tables.
    data = ensure_data(None if pack else build, library)
    tables = data / "hg_prediction_tables"
    root = ensure_pack(tree_id, library) if pack else data
    return YleafTree(
        id=tree_id,
        name=str(meta["name"]),
        version=str(meta["version"]),
        date=str(meta["date"]),
        license=str(meta["license"]),
        source=str(meta["source"]),
        tree_file=root / "tree.json" if pack else tables / TREE_FILES[tree_id],
        tables=tables,
        positions_root=root,
        pack=pack,
    )
