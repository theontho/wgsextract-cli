"""Gene lists for ``report``'s ClinVar section: built-in lists pinned in
``assets/report/gene_lists.json`` and downloaded once from the project's
GitHub release into the reference library, or a user's TSV or BED file.

A built-in list's file is checked against its pinned SHA-256 whenever it is
read. See scripts/build_report_gene_lists.py for how the lists are made.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import UsageError, WGSExtractError

MANIFEST: dict[str, Any] = json.loads(
    resources.files("wgsextract_cli")
    .joinpath("assets/report/gene_lists.json")
    .read_text(encoding="utf-8")
)
BUILDS = ("hg19", "hg38")
# Spans of a user's list that names no build: they are in the VCF's build.
ANY_BUILD = "any"


class GeneListUnavailable(WGSExtractError):
    """A built-in list is neither in the reference library nor downloadable."""


@dataclass(frozen=True)
class Gene:
    symbol: str
    category: str
    note: str
    spans: Mapping[str, tuple[str, int, int]]


@dataclass(frozen=True)
class GeneList:
    id: str
    name: str
    genes: tuple[Gene, ...]
    version: str | None = None
    citation: str | None = None
    url: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "citation": self.citation,
            "url": self.url,
            "genes": len(self.genes),
        }

    def needs_build(self) -> bool:
        return any(ANY_BUILD not in gene.spans for gene in self.genes)

    def genes_at(self, build: str | None, chrom: str, pos: int) -> list[Gene]:
        """Genes whose ``build`` span (or build-free span) holds the position."""
        contig = chrom_key(chrom)
        return [
            gene
            for gene in self.genes
            if (span := gene.spans.get(build or "") or gene.spans.get(ANY_BUILD))
            and span[0] == contig
            and span[1] <= pos <= span[2]
        ]

    def named(self, symbols: Iterable[str]) -> list[Gene]:
        """The list's genes among ``symbols``, in their order."""
        by_symbol = {gene.symbol.upper(): gene for gene in self.genes}
        return [by_symbol[s.upper()] for s in symbols if s.upper() in by_symbol]


def chrom_key(name: str) -> str:
    """A contig name without 'chr', with chrM as MT, to compare builds' styles."""
    bare = name[3:] if name.lower().startswith("chr") else name
    return "MT" if bare.upper() in ("M", "MT") else bare.upper()


def builtin_ids() -> tuple[str, ...]:
    return tuple(MANIFEST["lists"])


def default_id() -> str:
    return str(MANIFEST["default"])


def describe(spec: str | Path | None) -> dict[str, Any]:
    """A list's metadata without downloading it (a user's file is read)."""
    list_id = str(spec or default_id())
    meta = MANIFEST["lists"].get(list_id)
    if meta is None:
        return load(list_id).metadata()
    return {
        "id": list_id,
        **{k: meta[k] for k in ("name", "version", "citation", "url")},
        "genes": len(meta["genes"]),
    }


def builtin_symbols(list_id: str, category: str | None = None) -> tuple[str, ...]:
    """A built-in list's symbols (of one category), from the pinned manifest."""
    genes = MANIFEST["lists"][list_id]["genes"]
    return tuple(s for s, c in genes.items() if category in (None, c))


def check(spec: str | Path | None) -> None:
    """Fail early on a gene list that is neither built in nor a readable file."""
    list_id = str(spec or default_id())
    if list_id not in MANIFEST["lists"] and not Path(list_id).expanduser().is_file():
        raise UsageError(
            f"Gene list {list_id!r} is neither a built-in list "
            f"({', '.join(builtin_ids())}) nor a TSV or BED file."
        )


def load(spec: str | Path | None, library: Path | None = None) -> GeneList:
    """A built-in list (downloaded and verified if missing), or a user's
    TSV (column ``gene``; optional category, note and spans) or BED file."""
    list_id = str(spec or default_id())
    meta = MANIFEST["lists"].get(list_id)
    if meta is not None:
        path = ensure_builtin(list_id, library)
        genes = _read_tsv(path.read_text(encoding="utf-8"), path)
        return GeneList(
            list_id, meta["name"], genes, meta["version"], meta["citation"], meta["url"]
        )
    check(list_id)
    path = Path(list_id).expanduser().absolute()
    return _load_file(str(path), path.stat().st_mtime_ns)


@cache
def _load_file(path_text: str, _mtime: int) -> GeneList:
    path = Path(path_text)
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        text = stream.read()
    stem = path.name.lower().removesuffix(".gz")
    genes = _read_bed(text, path) if stem.endswith(".bed") else _read_tsv(text, path)
    if not genes:
        raise UsageError(f"Gene list {path} names no genes.")
    # Named by the file's name: reports keep absolute paths out.
    return GeneList(path.name, path.name, genes)


def ensure_builtin(list_id: str, library: Path | None = None) -> Path:
    """The built-in list's file in the reference library, downloaded and
    checked against its pinned SHA-256 if missing or changed."""
    meta = MANIFEST["lists"][list_id]
    folder = Path(library or resolve_reference_library()) / "report" / "gene_lists"
    path = folder / Path(meta["download"]).name
    if _verified(path, meta["sha256"]):
        return path
    folder.mkdir(parents=True, exist_ok=True)
    with build_lock(str(folder / f"{path.name}.lock")):
        if _verified(path, meta["sha256"]):
            return path
        logging.info(f"Downloading the {meta['name']} gene list...")
        try:
            ok = download_file(
                meta["download"],
                str(path),
                expected_sha256=meta["sha256"],
                resolve_release_digest=False,
            )
        except (OSError, WGSExtractError) as error:
            ok = False
            logging.debug("Gene list download failed: %s", error)
        if not ok or not _verified(path, meta["sha256"]):
            raise GeneListUnavailable(
                f"Cannot download the verified {meta['name']} gene list. If "
                "offline, connect once to populate the reference library, or "
                "pass a TSV or BED file with --gene-list."
            )
    return path


def _verified(path: Path, sha256: str) -> bool:
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == sha256


def _read_tsv(text: str, path: Path) -> tuple[Gene, ...]:
    rows = csv.DictReader(
        (line for line in text.splitlines() if line and not line.startswith("#")),
        delimiter="\t",
    )
    if "gene" not in (rows.fieldnames or ()):
        raise UsageError(f"Gene list {path} has no 'gene' column.")
    genes = []
    for number, row in enumerate(rows, start=2):
        try:
            spans = {
                build: (
                    chrom_key(row[f"{prefix}chrom"]),
                    int(row[f"{prefix}start"]),
                    int(row[f"{prefix}end"]),
                )
                for build, prefix in ((b, f"{b}_") for b in BUILDS)
                if row.get(f"{prefix}chrom")
            }
            if row.get("chrom"):
                spans[ANY_BUILD] = (
                    chrom_key(row["chrom"]),
                    int(row["start"]),
                    int(row["end"]),
                )
        except (TypeError, ValueError) as error:
            raise UsageError(f"Gene list {path}, row {number}: {error}") from error
        symbol = (row.get("gene") or "").strip()
        if symbol:
            genes.append(
                Gene(symbol, row.get("category") or "", row.get("note") or "", spans)
            )
    return tuple(genes)


def _read_bed(text: str, path: Path) -> tuple[Gene, ...]:
    """BED intervals (0-based starts) named by gene symbol in column 4; a
    gene's intervals merge into one span. BED names no build."""
    spans: dict[str, tuple[str, int, int]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.startswith(("#", "track", "browser")):
            continue
        cols = line.split("\t")
        if len(cols) < 4 or not cols[3].strip():
            raise UsageError(
                f"Gene list {path}, line {number}: a BED gene list needs the gene "
                "symbol in column 4."
            )
        try:
            chrom, start, end = chrom_key(cols[0]), int(cols[1]) + 1, int(cols[2])
        except ValueError as error:
            raise UsageError(f"Gene list {path}, line {number}: {error}") from error
        symbol = cols[3].strip()
        old = spans.get(symbol)
        if old and old[0] != chrom:
            raise UsageError(
                f"Gene list {path}: {symbol} is on two chromosomes ({old[0]}, {chrom})."
            )
        spans[symbol] = (
            (chrom, min(old[1], start), max(old[2], end))
            if old
            else (chrom, start, end)
        )
    return tuple(Gene(s, "", "", {ANY_BUILD: span}) for s, span in spans.items())
