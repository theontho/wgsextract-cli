# SPDX-License-Identifier: GPL-3.0-or-later
"""A user's own Y trees: config.toml ``[y_trees.<id>]`` entries and ``--tree-file``.

A custom tree is a Yleaf-format tree JSON (each node mapped to its children,
from ``ROOT (Y-Chromosome "Adam")``) plus a marker table per build (Yleaf's
seven tab-separated columns). Files are local paths or ``{url, sha256}``
tables; a remote file is fetched once into the reference library and
verified against its pinned SHA-256. Both files are checked against the
schema before use, so a bad tree fails with the file, line and fix.
"""

import csv
import difflib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from wgsextract_cli.core.download_progress import redact_urls
from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.paths import expand_path
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import WGSExtractError

from .data import file_sha256
from .predict import QcRules
from .tree import Tree
from .trees import (
    BUILDS,
    DEFAULT_TREE,
    MANIFEST,
    QC_PROFILES,
    TREE_IDS,
    Panel,
    YleafTree,
    shared_qc_rules,
)

ENTRY_KEYS = (
    "name",
    "version",
    "date",
    "license",
    "source",
    "tree",
    "positions",
    "positions_ancient",
    "qc",
)
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
MARKER_COLUMNS = (
    "chromosome, marker, haplogroup, position, mutation, ancestral, derived"
)
BASES = frozenset("ACGT")
# Tree() reads the tree recursively; keep well inside Python's recursion limit.
MAX_DEPTH = 500


@dataclass(frozen=True)
class FileRef:
    location: str
    """A local path, or an http(s) URL."""
    sha256: str | None

    @property
    def remote(self) -> bool:
        return self.location.startswith(("https://", "http://"))


@dataclass(frozen=True)
class CustomTreeSpec:
    id: str
    name: str
    version: str
    date: str
    license: str
    source: str
    tree: FileRef
    positions: dict[str, FileRef]
    positions_ancient: dict[str, FileRef]
    qc: str


def configured_trees() -> dict[str, Any]:
    """config.toml's ``y_trees`` table."""
    from wgsextract_cli.core.config import settings

    trees = settings.get("y_trees") or {}
    if not isinstance(trees, dict):
        raise WGSExtractError(_where("y_trees", "must be a TOML table of trees."))
    return trees


def available_tree_ids() -> list[str]:
    return [*TREE_IDS, *(i for i in configured_trees() if i not in TREE_IDS)]


def configured_tree_spec() -> dict[str, Any]:
    """The tree a run without --tree uses, as resume-journal parameters:
    config.toml's y_tree (else the default) with its pinned version or its
    ``[y_trees.<id>]`` entry."""
    from wgsextract_cli.core.config import settings

    tree_id = str(settings.get("y_tree") or DEFAULT_TREE)
    if tree_id in MANIFEST["trees"]:
        return {"id": tree_id, "version": MANIFEST["trees"][tree_id]["version"]}
    return {"id": tree_id, "entry": configured_trees().get(tree_id)}


def _where(section: str, problem: str) -> str:
    from wgsextract_cli.core.config import get_config_path

    return f"{get_config_path()} [{section}]: {problem}"


def _resolve_path(value: str) -> str:
    from wgsextract_cli.core.config import get_config_path

    path = Path(expand_path(value))
    # A relative path in config.toml is relative to config.toml's folder.
    return str(path if path.is_absolute() else get_config_path().parent / path)


def _file_ref(section: str, value: Any) -> FileRef:
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict):
        raise WGSExtractError(
            _where(section, 'expected a path, or {url = "...", sha256 = "..."}.')
        )
    unknown = set(value) - {"path", "url", "sha256"}
    if unknown or ("path" in value) == ("url" in value):
        raise WGSExtractError(
            _where(
                section,
                "give exactly one of path or url (with sha256), e.g. "
                '{url = "https://example.org/tree.json", sha256 = "<64 hex digits>"}.',
            )
        )
    sha256 = value.get("sha256")
    if sha256 is not None and (
        not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256)
    ):
        raise WGSExtractError(_where(section, "sha256 must be 64 hex digits."))
    if "url" in value:
        url = value["url"]
        if not isinstance(url, str) or urlparse(url).scheme not in ("https", "http"):
            raise WGSExtractError(_where(section, "url must be an http(s) URL."))
        if sha256 is None:
            raise WGSExtractError(
                _where(
                    section,
                    "a url needs its sha256, so the file can't change unnoticed "
                    "(sha256sum prints it).",
                )
            )
        return FileRef(url, sha256.lower())
    if not isinstance(value["path"], str):
        raise WGSExtractError(_where(section, "path must be a string."))
    return FileRef(_resolve_path(value["path"]), sha256 and sha256.lower())


def _build_files(section: str, value: Any) -> dict[str, FileRef]:
    if not isinstance(value, dict):
        raise WGSExtractError(
            _where(section, 'expected a table of builds, e.g. hg38 = "markers.txt".')
        )
    unknown = set(value) - set(BUILDS)
    if unknown:
        raise WGSExtractError(
            _where(
                section,
                f"unknown build {sorted(unknown)[0]!r}; use {', '.join(BUILDS)}.",
            )
        )
    return {build: _file_ref(f"{section}.{build}", ref) for build, ref in value.items()}


def parse_entry(tree_id: str, raw: Any) -> CustomTreeSpec:
    """Check a ``[y_trees.<id>]`` entry and resolve its paths."""
    section = f"y_trees.{tree_id}"
    if not ID_PATTERN.fullmatch(tree_id):
        raise WGSExtractError(
            _where(section, "tree ids use letters, digits, '.', '_' and '-'.")
        )
    if tree_id in TREE_IDS:
        raise WGSExtractError(
            _where(section, f"{tree_id!r} is a built-in tree; choose another id.")
        )
    if not isinstance(raw, dict):
        raise WGSExtractError(_where(section, "expected a table."))
    for key in raw:
        if key not in ENTRY_KEYS:
            close = difflib.get_close_matches(key, ENTRY_KEYS, n=1)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise WGSExtractError(_where(section, f"unknown key {key!r}.{hint}"))
    for key in ("version", "tree", "positions"):
        if key not in raw:
            raise WGSExtractError(_where(section, f"{key!r} is required."))
    for key in ("name", "version", "date", "license", "source", "qc"):
        if key in raw and not isinstance(raw[key], str):
            raise WGSExtractError(_where(section, f"{key!r} must be a string."))
    qc = raw.get("qc", "none")
    if qc not in (*QC_PROFILES, "none"):
        raise WGSExtractError(
            _where(section, f"qc must be none or one of {', '.join(QC_PROFILES)}.")
        )
    tree = _file_ref(f"{section}.tree", raw["tree"])
    positions = _build_files(f"{section}.positions", raw["positions"])
    if not positions:
        raise WGSExtractError(
            _where(f"{section}.positions", "list at least one build's markers.")
        )
    return CustomTreeSpec(
        id=tree_id,
        name=raw.get("name", tree_id),
        version=raw["version"],
        date=raw.get("date", ""),
        license=raw.get("license", "unspecified"),
        source=raw.get("source", redact_urls(tree.location)),
        tree=tree,
        positions=positions,
        positions_ancient=_build_files(
            f"{section}.positions_ancient", raw.get("positions_ancient", {})
        ),
        qc=qc,
    )


def fetch(ref: FileRef, library: Path | None = None) -> Path:
    """A local file, or a remote one cached in the reference library; either
    checked against its SHA-256 when one is pinned."""
    if not ref.remote:
        path = Path(ref.location)
        if not path.is_file():
            raise WGSExtractError(f"Custom Y tree file not found: {path}")
        if ref.sha256 and file_sha256(path) != ref.sha256:
            raise WGSExtractError(
                f"{path} does not match its pinned sha256 {ref.sha256}; it has "
                "changed since the entry was written."
            )
        return path
    assert ref.sha256 is not None
    name = Path(urlparse(ref.location).path).name or "file"
    base = Path(library or resolve_reference_library())
    folder = base / "yleaf" / "trees" / "custom" / ref.sha256
    path = folder / name
    if path.is_file() and file_sha256(path) == ref.sha256:
        return path
    folder.mkdir(parents=True, exist_ok=True)
    with build_lock(str(folder / "download.lock")):
        if path.is_file() and file_sha256(path) == ref.sha256:
            return path
        shown = redact_urls(ref.location)
        logging.info(f"Downloading custom Y tree file {shown}...")
        if not download_file(
            ref.location,
            str(path),
            expected_sha256=ref.sha256,
            resolve_release_digest=False,
        ):
            raise WGSExtractError(
                f"Cannot download {shown} with sha256 {ref.sha256}. Check "
                "the URL and the pinned sha256; if offline, connect once."
            )
    return path


def tree_nodes(path: Path) -> set[str]:
    """The nodes reachable from the root of a Yleaf tree JSON; raise if malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WGSExtractError(f"{path}: not a valid JSON tree ({error}).") from error
    root = Tree.ROOT_KEY
    example = f'{{"{root}": ["A0-T"], "A0-T": ["A0", "A1"], ...}}'
    if not isinstance(data, dict) or root not in data:
        raise WGSExtractError(
            f'{path}: a Yleaf tree maps each node to its children, starting at "'
            f'{root}", e.g. {example}.'
        )
    for node, children in data.items():
        if not isinstance(children, list) or not all(
            isinstance(child, str) for child in children
        ):
            raise WGSExtractError(
                f'{path}: node "{node}" must map to a list of child names.'
            )
    parents: dict[str, str] = {}
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise WGSExtractError(
                f'{path}: the tree is deeper than {MAX_DEPTH} levels at "{node}".'
            )
        for child in data[node]:
            if child not in data:
                raise WGSExtractError(
                    f'{path}: "{child}", a child of "{node}", has no entry of its '
                    f'own; list a leaf with no children as "{child}": [].'
                )
            if child in parents or child == root:
                raise WGSExtractError(
                    f'{path}: node "{child}" is a child of both '
                    f'"{parents.get(child, "(it is the root)")}" and "{node}"; '
                    "each node needs exactly one parent."
                )
            parents[child] = node
            stack.append((child, depth + 1))
    return {root, *parents}


def check_markers(path: Path, nodes: set[str], tree: str) -> None:
    """Raise unless ``path`` is a Yleaf marker table naming nodes of ``tree``."""
    missing: list[tuple[int, str]] = []
    count = 0
    with path.open(encoding="utf-8", newline="") as stream:
        for line, row in enumerate(csv.reader(stream, delimiter="\t"), 1):
            where = f"{path}:{line}"
            if len(row) != 7:
                raise WGSExtractError(
                    f"{where}: expected 7 tab-separated columns ({MARKER_COLUMNS}); "
                    f"found {len(row)}."
                )
            chromosome, _, haplogroup, position, _, ancestral, derived = row
            if chromosome.lower() not in ("y", "chry"):
                raise WGSExtractError(
                    f"{where}: chromosome {chromosome!r} is not chrY; Y tree "
                    "markers must be on chrY."
                )
            if not position.isdigit() or int(position) < 1:
                raise WGSExtractError(
                    f"{where}: position {position!r} is not a positive integer."
                )
            if ancestral not in BASES or derived not in BASES or ancestral == derived:
                raise WGSExtractError(
                    f"{where}: ancestral and derived alleles must be two "
                    f"different bases of A, C, G, T; got {ancestral!r}, {derived!r}."
                )
            if haplogroup not in nodes:
                missing.append((line, haplogroup))
            count += 1
    if not count:
        raise WGSExtractError(f"{path}: the marker table is empty.")
    if missing:
        line, haplogroup = missing[0]
        raise WGSExtractError(
            f"{path}: {len(missing)} of {count} markers name a haplogroup that "
            f"is not in {tree}, e.g. line {line}: {haplogroup!r}. Each marker's "
            "third column must be a node of the tree."
        )


def _check_backbone(rules: QcRules, nodes: set[str], spec: CustomTreeSpec) -> None:
    assert rules.backbone is not None
    groups = {
        line.strip()
        for line in (rules.backbone / "Intermediates.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and "~" not in line
    }
    absent = sorted(groups - nodes)
    # QC1 scores 0 for any call outside the backbone, so most must be present.
    if len(absent) * 2 > len(groups):
        raise WGSExtractError(
            _where(
                f"y_trees.{spec.id}",
                f"qc = {spec.qc!r} checks calls against Yleaf's {spec.qc} backbone "
                f"groups, but {len(absent)} of {len(groups)} are not nodes of this "
                f"tree (e.g. {', '.join(absent[:3])}). Use qc = 'none', or name "
                "the tree's nodes as that tree does.",
            )
        )


def check_tree_id(tree_id: str) -> None:
    """Raise unless ``tree_id`` is a built-in tree or a valid config.toml entry."""
    trees = configured_trees()
    if tree_id in TREE_IDS:
        if tree_id in trees:
            parse_entry(tree_id, trees[tree_id])
        return
    if tree_id not in trees:
        raise WGSExtractError(
            f"Unknown Y tree {tree_id!r}; choose one of "
            f"{', '.join(available_tree_ids())}, or add a [y_trees.{tree_id}] "
            "table to config.toml."
        )
    parse_entry(tree_id, trees[tree_id])


def ensure_custom_tree(
    tree_id: str,
    build: str,
    library: Path | None = None,
    panel: Panel | None = "regular",
) -> YleafTree:
    """A config.toml tree for ``build``, with its tree file and the ``panel``
    marker table (None: none) fetched and checked."""
    check_tree_id(tree_id)
    spec = parse_entry(tree_id, configured_trees()[tree_id])
    key = "positions_ancient" if panel == "ancient" else "positions"
    tables = spec.positions_ancient if panel == "ancient" else spec.positions
    if panel is not None and build not in tables:
        listed = ", ".join(tables) or "no build"
        raise WGSExtractError(
            _where(
                f"y_trees.{tree_id}.{key}",
                f"no {build} marker table (it lists {listed}). "
                f"Add {key}.{build} for this {build} input.",
            )
        )
    tree_file = fetch(spec.tree, library)
    nodes = tree_nodes(tree_file)
    selected = {build: fetch(tables[build], library)} if panel is not None else {}
    for path in selected.values():
        check_markers(path, nodes, f"the {tree_id} tree")
    positions = selected if panel == "regular" else {}
    ancient = selected if panel == "ancient" else {}
    rules = QcRules()
    if spec.qc != "none":
        rules = shared_qc_rules(spec.qc, library)
        _check_backbone(rules, nodes, spec)
    return YleafTree(
        id=spec.id,
        name=spec.name,
        version=spec.version,
        date=spec.date,
        license=spec.license,
        source=spec.source,
        tree_file=tree_file,
        positions_files=positions,
        ancient_files=ancient,
        qc=rules,
        origin="custom-tree",
    )


def file_tree(tree_file: Path, positions: Path, build: str) -> YleafTree:
    """A one-off ``--tree-file`` tree with the ``--pos-file`` markers; no QC1."""
    nodes = tree_nodes(tree_file)
    check_markers(positions, nodes, str(tree_file))
    return YleafTree(
        id="tree-file",
        name=tree_file.name,
        version=file_sha256(tree_file)[:12],
        date="",
        license="unspecified",
        source=str(tree_file),
        tree_file=tree_file,
        positions_files={build: positions},
        qc=QcRules(),
        origin="custom-tree",
    )
