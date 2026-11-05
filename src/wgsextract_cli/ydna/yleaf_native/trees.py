# SPDX-License-Identifier: GPL-3.0-or-later
"""The Yleaf Y haplogroup tree registry: each tree's version, license, files
and QC rules.

``trees.json`` lists the built-in trees. Yleaf v4.1.6's data archives carry the
yfull_v10, ftdna and isogg trees and their marker tables. The yfull tree is a
separate pack built from YFull's YTree by ``scripts/build_yfull_tree.py``,
pinned by SHA-256 and verified into the reference library on first use. A
user's own trees come from config.toml ``[y_trees.<id>]`` tables or
``--tree-file`` (see ``custom_trees``).
"""

import json
import logging
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wgsextract_cli.core.dev_download_cache import process_umask
from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import WGSExtractError

from .data import ensure_data, extract_archive, verified
from .predict import QcRules

BUILDS = ("hg19", "hg38", "t2t")

MANIFEST: dict[str, Any] = json.loads(
    Path(__file__).with_name("trees.json").read_text(encoding="utf-8")
)
TREE_IDS: tuple[str, ...] = tuple(MANIFEST["trees"])
"""The built-in trees."""
DEFAULT_TREE: str = MANIFEST["default"]
QC_PROFILES: tuple[str, ...] = tuple(MANIFEST["qc"])


@dataclass(frozen=True)
class YleafTree:
    id: str
    name: str
    version: str
    date: str
    license: str
    source: str
    tree_file: Path
    positions_files: dict[str, Path]
    """Marker table per build."""
    qc: QcRules
    origin: str
    """Where the marker tables come from: tree-pack, yleaf-data or custom-tree."""
    root: Path | None = None
    """The folder marker table identifiers are relative to (None: file names)."""
    ancient_files: dict[str, Path] = field(default_factory=dict)

    def positions(self, build: str, ancient: bool = False) -> Path:
        if ancient and build in self.ancient_files:
            return self.ancient_files[build]
        if build not in self.positions_files:
            raise WGSExtractError(
                f"Y tree {self.id!r} has no {build} marker table; it covers "
                f"{', '.join(self.positions_files) or 'no build'}."
            )
        return self.positions_files[build]

    def identifier(self, path: Path) -> str:
        """How run.json names a marker table of this tree."""
        if self.root is not None and path.is_relative_to(self.root):
            return path.relative_to(self.root).as_posix()
        return path.name

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
    notes = [meta["date"]] if meta["date"] and meta["date"] != meta["version"] else []
    unknown = meta["license"] == "unspecified"
    notes.append("license unspecified" if unknown else meta["license"])
    return f"{meta['name']} {meta['version']} ({', '.join(notes)})"


def tree_metadata(tree_id: str) -> dict[str, Any]:
    """A built-in tree's trees.json entry."""
    meta: dict[str, Any] | None = MANIFEST["trees"].get(tree_id)
    if meta is None:
        raise WGSExtractError(
            f"Unknown Yleaf tree {tree_id!r}; choose one of {', '.join(TREE_IDS)}."
        )
    return meta


def qc_rules(profile: str, data: Path) -> QcRules:
    """``profile``'s QC1 backbone tables in Yleaf's data folder ``data``."""
    rules = MANIFEST["qc"][profile]
    tables = data / "hg_prediction_tables"
    return QcRules(
        backbone=tables / rules["backbone"],
        tables=tables / rules["tables"],
        major_tree_list=rules["major_tree_list"],
        ftdna_rules=rules["ftdna_rules"],
    )


def shared_qc_rules(profile: str, library: Path | None = None) -> QcRules:
    """``qc_rules`` in Yleaf's build-independent tables, downloaded if missing."""
    return qc_rules(profile, ensure_data(None, library))


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
    """The tree and the ``build`` marker tables it needs, downloaded if missing.

    ``tree_id`` is a built-in tree or a config.toml ``[y_trees.<id>]`` entry.
    """
    if build not in BUILDS:
        raise WGSExtractError(f"Unsupported Yleaf build: {build}")
    if tree_id not in MANIFEST["trees"]:
        from .custom_trees import ensure_custom_tree

        return ensure_custom_tree(tree_id, build, library)
    meta = tree_metadata(tree_id)
    pack = meta["data"] == "pack"
    # A pack has its own marker tables; Yleaf's archive supplies QC1's tables.
    data = ensure_data(None if pack else build, library)
    root = ensure_pack(tree_id, library) if pack else data
    return YleafTree(
        id=tree_id,
        name=str(meta["name"]),
        version=str(meta["version"]),
        date=str(meta["date"]),
        license=str(meta["license"]),
        source=str(meta["source"]),
        tree_file=root / meta["tree"],
        positions_files={
            b: root / meta["positions"].format(build=b) for b in meta["builds"]
        },
        ancient_files={
            b: root / meta["positions_ancient"].format(build=b) for b in meta["builds"]
        },
        qc=qc_rules(meta["qc"], data),
        origin="tree-pack" if pack else "yleaf-data",
        root=root,
    )
