"""Pinned Haplogrep 3 phylogenetic trees, downloaded and verified on first use.

Haplogrep 3 ships without trees and installs them into its own folder. Instead,
the pinned upstream tree archives in ``haplogrep_trees.json`` are verified into
the reference library, and each run points Haplogrep at one through a private
configuration file, so the Pixi environment is never modified.
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from wgsextract_cli.core.dev_download_cache import process_umask
from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import WGSExtractError

MANIFEST: dict[str, Any] = json.loads(
    Path(__file__).with_name("haplogrep_trees.json").read_text(encoding="utf-8")
)
TREE_IDS: tuple[str, ...] = tuple(MANIFEST["trees"])
DEFAULT_TREE: str = MANIFEST["default"]
CONFIG_FILENAME = "haplogrep3.yaml"


@dataclass(frozen=True)
class HaplogrepTree:
    id: str
    version: str
    name: str
    path: Path

    @property
    def spec(self) -> str:
        """The ``id@version`` Haplogrep's ``--tree`` takes."""
        return f"{self.id}@{self.version}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(root: Path, files: dict[str, Any]) -> bool:
    for name, meta in files.items():
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != meta["size"]
            or _sha256(path) != meta["sha256"]
        ):
            return False
    return True


def _extract(archive: Path, root: Path, files: dict[str, Any]) -> None:
    """Extract only the manifest's regular files; never follow archive paths."""
    with zipfile.ZipFile(archive) as stream:
        seen = set()
        for entry in stream.infolist():
            if entry.is_dir():
                continue
            name = PurePosixPath(entry.filename)
            if (
                entry.filename not in files
                or entry.filename in seen
                or name.is_absolute()
                or ".." in name.parts
                or entry.file_size != files[entry.filename]["size"]
            ):
                raise WGSExtractError("Unexpected file in Haplogrep tree archive.")
            seen.add(entry.filename)
            target = root / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.open(entry) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
        if seen != set(files):
            raise WGSExtractError("Incomplete Haplogrep tree archive.")
    if not _verified(root, files):
        raise WGSExtractError("Haplogrep tree failed SHA-256 verification.")


def configured_tree_spec() -> str:
    """``id@version`` of the tree a run without --tree uses: config.toml's
    haplogrep_tree, else the default, at its pinned version."""
    from wgsextract_cli.core.config import settings

    tree_id = str(settings.get("haplogrep_tree") or DEFAULT_TREE)
    meta = MANIFEST["trees"].get(tree_id, {})
    return f"{tree_id}@{meta.get('version', '')}"


def ensure_tree(tree_id: str, library: str | Path | None = None) -> HaplogrepTree:
    """The pinned tree, downloaded into the reference library if missing."""
    meta = MANIFEST["trees"].get(tree_id)
    if meta is None:
        raise WGSExtractError(
            f"Unknown Haplogrep tree {tree_id!r}; choose one of {', '.join(TREE_IDS)}."
        )
    version = str(meta["version"])
    base = Path(library or resolve_reference_library())
    root = base / "haplogrep3" / "trees" / tree_id / version
    tree = HaplogrepTree(tree_id, version, str(meta["name"]), root / "tree.yaml")
    files: dict[str, Any] = meta["files"]
    if _verified(root, files):
        return tree
    root.parent.mkdir(parents=True, exist_ok=True)
    with build_lock(str(root.parent / f"{version}.lock")):
        if _verified(root, files):
            return tree
        logging.info(f"Downloading Haplogrep tree {tree.spec} ({tree.name})...")
        archive = root.parent / f"{tree_id}-{version}.zip"
        try:
            if not download_file(
                meta["url"],
                str(archive),
                expected_sha256=meta["sha256"],
                resolve_release_digest=False,
            ):
                raise WGSExtractError(
                    f"Cannot download the verified Haplogrep tree {tree.spec}. If "
                    "offline, connect once to populate the reference library."
                )
            staging = Path(tempfile.mkdtemp(prefix="unpack-", dir=root.parent))
            try:
                _extract(archive, staging, files)
                # mkdtemp makes 0700; publish readable to a shared library's users.
                if os.name != "nt":
                    os.chmod(staging, 0o777 & ~process_umask())
                shutil.rmtree(root, ignore_errors=True)
                staging.replace(root)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        except (OSError, zipfile.BadZipFile) as error:
            raise WGSExtractError(
                f"Unable to prepare Haplogrep tree {tree.spec}: {error}"
            ) from error
        finally:
            archive.unlink(missing_ok=True)
    return tree


def write_config(tree: HaplogrepTree | None, home: Path) -> None:
    """Write a Haplogrep 3 configuration that loads only ``tree`` (or, for a
    version probe, no tree and no tree repository) into ``home``.

    Haplogrep reads ``haplogrep3.yaml`` from its working directory, else the
    one beside its jar, whose trees it downloads at startup; it loads a tree
    listed by the path of its tree.yaml.
    """
    trees = f"\n  - {json.dumps(tree.path.absolute().as_posix())}" if tree else " []"
    (home / CONFIG_FILENAME).write_text(
        f"repositories: []\nphylotrees:{trees}\n",
        encoding="utf-8",
        newline="\n",
    )
