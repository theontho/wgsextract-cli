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


Span = tuple[str, int, int]


class GeneListUnavailable(WGSExtractError):
    """A built-in list is neither in the reference library nor downloadable."""


@dataclass(frozen=True)
class Gene:
    symbol: str
    category: str
    note: str
    # Per build (or ANY_BUILD), every interval of the gene.
    spans: Mapping[str, tuple[Span, ...]]


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
            # A user's file has none: empty, as report/1 keeps them strings.
            "version": self.version or "",
            "citation": self.citation or "",
            "url": self.url or "",
            "genes": len(self.genes),
        }

    def needs_build(self) -> bool:
        return any(ANY_BUILD not in gene.spans for gene in self.genes)

    def has_spans(self, build: str | None) -> bool:
        """Whether any gene has a span to place a ``build`` position by."""
        return any(
            (build in gene.spans if build else False) or ANY_BUILD in gene.spans
            for gene in self.genes
        )

    def genes_at(self, build: str | None, chrom: str, pos: int) -> list[Gene]:
        """Genes with a ``build`` (or build-free) interval holding the position."""
        contig = chrom_key(chrom)
        return [
            gene
            for gene in self.genes
            if any(
                c == contig and start <= pos <= end
                for c, start, end in (
                    gene.spans.get(build or "") or gene.spans.get(ANY_BUILD) or ()
                )
            )
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
        try:
            path = ensure_builtin(list_id, library)
            genes = _read_tsv(path.read_text(encoding="utf-8"), path)
        except GeneListUnavailable:
            raise
        except (OSError, WGSExtractError) as error:
            raise GeneListUnavailable(
                f"Cannot use the {meta['name']} gene list: {error}. Pass a TSV or "
                "BED file with --gene-list."
            ) from error
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
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            text = stream.read()
    except (OSError, EOFError, UnicodeDecodeError) as error:
        raise UsageError(f"Cannot read gene list {path}: {error}") from error
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
    genes: list[Gene] = []
    for number, row in enumerate(rows, start=2):
        try:
            spans = {
                build: (_span(row, prefix),)
                for build, prefix in (*((b, f"{b}_") for b in BUILDS), (ANY_BUILD, ""))
                if row.get(f"{prefix}chrom")
            }
        except (KeyError, TypeError, ValueError) as error:
            raise UsageError(
                f"Gene list {path}, row {number}: bad span ({error})."
            ) from error
        symbol = (row.get("gene") or "").strip()
        if symbol:
            genes.append(
                Gene(symbol, row.get("category") or "", row.get("note") or "", spans)
            )
    return _merged(genes)


def _merged(genes: list[Gene]) -> tuple[Gene, ...]:
    """One gene per symbol, with the intervals of all its rows (the first
    row's category and note)."""
    by_symbol: dict[str, Gene] = {}
    for gene in genes:
        old = by_symbol.get(gene.symbol)
        if old is None:
            by_symbol[gene.symbol] = gene
            continue
        spans = {b: tuple(old.spans.get(b, ())) for b in (*old.spans, *gene.spans)}
        for build, intervals in gene.spans.items():
            spans[build] += tuple(intervals)
        by_symbol[gene.symbol] = Gene(old.symbol, old.category, old.note, spans)
    return tuple(by_symbol.values())


def _span(row: dict[str, str], prefix: str) -> Span:
    """A row's 1-based, inclusive ``<prefix>chrom/start/end`` span."""
    start, end = int(row[f"{prefix}start"]), int(row[f"{prefix}end"])
    if not 1 <= start <= end:
        raise ValueError(f"{prefix}start {start} and {prefix}end {end}")
    return chrom_key(row[f"{prefix}chrom"]), start, end


def _read_bed(text: str, path: Path) -> tuple[Gene, ...]:
    """BED intervals (0-based starts) named by gene symbol in column 4; a
    gene keeps every interval, on any chromosome (PAR genes, repeated
    symbols). BED names no build."""
    genes = []
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
            if not 1 <= start <= end:
                raise ValueError(f"start {cols[1]} and end {cols[2]}")
        except ValueError as error:
            raise UsageError(
                f"Gene list {path}, line {number}: bad interval ({error})."
            ) from error
        genes.append(Gene(cols[3].strip(), "", "", {ANY_BUILD: ((chrom, start, end),)}))
    return _merged(genes)
