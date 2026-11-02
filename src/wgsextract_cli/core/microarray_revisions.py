"""Identify the vendor, chip and revision of a consumer microarray raw-data file.

Vendors have changed marker content within one advertised chip version without
renaming it. A revision is fingerprinted by its marker-ID multiset (exact),
its marker sites (ID, chromosome, position; this separates genome builds) and
a bottom-k MinHash sketch of its IDs (similarity to unseen layouts). Only
derived fingerprints are packaged, never genotypes.
"""

from __future__ import annotations

import base64
import csv
import functools
import gzip
import hashlib
import heapq
import io
import json
import math
import re
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from wgsextract_cli.core.microarray_utils import (
    _resolve_templates_for_format,
    get_template_format,
    template_rows,
    vendor_chrom,
)
from wgsextract_cli.core.utils import WGSExtractError

CATALOGUE = (
    Path(__file__).resolve().parents[1] / "assets/microarray/revisions/catalogue.json"
)
SKETCH_SIZE = 256
_FORMAT = "wgsextract-chip-revisions"
# Second-best scores within these margins make a closest match ambiguous.
_EXACT_MARGIN = 0.005
_HEADER_LIMIT = 200

_VENDOR_CUES = (
    ("23andMe", re.compile(r"23andMe", re.I)),
    ("AncestryDNA", re.compile(r"AncestryDNA", re.I)),
    ("MyHeritage", re.compile(r"MyHeritage", re.I)),
    ("Living DNA", re.compile(r"Living\s?DNA", re.I)),
    ("FamilyTreeDNA", re.compile(r"Family\s?Tree\s?DNA|FTDNA", re.I)),
)
_BUILD_CUES = (
    ("NCBI36", re.compile(r"\bbuild\s*36\b|\bNCBI\s*36\b|\bhg18\b", re.I)),
    ("GRCh37", re.compile(r"\bbuild\s*37\b|\bGRCh\s*37\b|\bhg19\b", re.I)),
    ("GRCh38", re.compile(r"\bbuild\s*38\b|\bGRCh\s*38\b|\bhg38\b", re.I)),
)
_CHIP_CUES = (
    re.compile(r"array version:?\s*([A-Za-z0-9._-]+)", re.I),
    re.compile(r"##chip=\s*([A-Za-z0-9._-]+)", re.I),
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan feb mar apr may jun jul aug sep oct nov dec").split(), 1
    )
}
_DATE_CUES = (
    (re.compile(r"\b(\d{4})-(\d{2})-\d{2}"), (1, 2)),
    (re.compile(r"\b(\d{1,2})/\d{1,2}/(\d{4})"), (2, 1)),
    (
        re.compile(r"\b([A-Z][a-z]{2})\w*\s+\d{1,2}\s+[\d:]+\s+(?:\w+\s+)?(\d{4})"),
        (2, 1),
    ),
)
_COLUMN_LABELS = {"rsid", "# rsid", "snp", "name", "snp name", "marker"}


def _hash64(marker: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(marker.encode(), digest_size=8).digest(), "big"
    )


def bottom_k(markers: Iterable[str], k: int = SKETCH_SIZE) -> tuple[int, ...]:
    """The k smallest 64-bit hashes of the distinct marker IDs, ascending."""
    return tuple(sorted(heapq.nsmallest(k, map(_hash64, set(markers)))))


def encode_sketch(sketch: Sequence[int]) -> str:
    return base64.b64encode(b"".join(v.to_bytes(8, "big") for v in sketch)).decode()


def decode_sketch(value: str) -> tuple[int, ...]:
    raw = base64.b64decode(value, validate=True)
    if len(raw) % 8:
        raise ValueError("Invalid sketch length")
    values = tuple(int.from_bytes(raw[i : i + 8], "big") for i in range(0, len(raw), 8))
    if list(values) != sorted(set(values)):
        raise ValueError("Sketch values must be distinct and ascending")
    return values


def estimate_jaccard(a: Sequence[int], b: Sequence[int]) -> float:
    """Bottom-k estimate of the Jaccard similarity of two ID sets."""
    k = min(len(a), len(b))
    if not k:
        return 0.0
    union = heapq.nsmallest(k, set(a) | set(b))
    shared = set(a) & set(b)
    return sum(value in shared for value in union) / k


def _multiset_sha256(values: list[str]) -> str:
    values.sort()
    values.append("")
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    markers: int
    ids_sha256: str
    sites_sha256: str
    sketch: tuple[int, ...]
    ids: frozenset[str] = field(repr=False, compare=False)


def fingerprint(rows: Iterable[tuple[str, str, str]]) -> Fingerprint:
    """Fingerprint (ID, chromosome, position) rows; row order is ignored."""
    rows = list(rows)
    if not rows:
        raise WGSExtractError("The microarray file contains no markers.")
    ids = frozenset(marker for marker, _, _ in rows)
    return Fingerprint(
        len(rows),
        _multiset_sha256([marker for marker, _, _ in rows]),
        _multiset_sha256(
            [f"{marker}\t{vendor_chrom(chrom)}\t{pos}" for marker, chrom, pos in rows]
        ),
        bottom_k(ids),
        ids,
    )


@dataclass(frozen=True)
class RawExport:
    vendor: str | None
    declared_chip: str | None
    declared_build: str | None
    export_month: str | None
    layout: str
    fingerprint: Fingerprint


def _header_month(text: str) -> str | None:
    for pattern, (year_group, month_group) in _DATE_CUES:
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group(year_group))
        month_text = match.group(month_group)
        month = (
            _MONTHS.get(month_text[:3].lower())
            if month_text.isalpha()
            else int(month_text)
        )
        if month and 1 <= month <= 12 and 2000 <= year <= 2100:
            return f"{year:04d}-{month:02d}"
    return None


def _first(cues: Sequence[tuple[str, re.Pattern[str]]], text: str) -> str | None:
    return next((name for name, pattern in cues if pattern.search(text)), None)


def decode_text(data: bytes) -> str:
    """Raw-data text: UTF-8 (optionally with BOM) or, as some re-saved files are, UTF-16."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _read_bytes(path: Path) -> bytes:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and not Path(info.filename).name.startswith(".")
                and "__MACOSX" not in info.filename
            ]
            if len(members) != 1:
                raise WGSExtractError(
                    "A zipped microarray file must contain exactly one data file."
                )
            return archive.read(members[0])
    data = path.read_bytes()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def read_raw_export(path: str | Path) -> RawExport:
    """Parse a raw-data export (plain, gzip or single-file ZIP) without genotypes."""
    path = Path(path)
    try:
        text = decode_text(_read_bytes(path))
        return parse_raw_export(io.StringIO(text), path.name)
    except (
        OSError,
        EOFError,
        UnicodeDecodeError,
        csv.Error,
        zlib.error,
        zipfile.BadZipFile,
    ) as error:
        raise WGSExtractError(f"Cannot read microarray file {path}: {error}") from error


def parse_raw_export(stream: TextIO, name: str) -> RawExport:
    lines = stream.read().splitlines()
    header = [line.strip() for line in lines[:_HEADER_LIMIT] if line[:1] == "#"]
    body = [line for line in lines if line[:1] != "#" and line.strip()]
    del lines
    if not body:
        raise WGSExtractError(f"{name} contains no markers.")
    delimiter = "\t" if "\t" in body[0] else ","
    fields = (
        list(csv.reader(body, delimiter=delimiter))
        if any('"' in line for line in body)
        else [line.split(delimiter) for line in body]
    )
    del body
    if fields[0] and fields[0][0].strip().lower() in _COLUMN_LABELS:
        header.append(delimiter.join(fields.pop(0)))
    if not fields:
        raise WGSExtractError(f"{name} contains no markers.")
    for number, row in enumerate(fields, 1):
        if len(row) < 3 or not row[0].strip():
            raise WGSExtractError(
                f"{name}: marker row {number} is not ID, chromosome, position, ..."
            )
    text = "\n".join(header)
    chip = next((m.group(1) for p in _CHIP_CUES if (m := p.search(text))), None)
    return RawExport(
        vendor=_first(_VENDOR_CUES, text),
        declared_chip=chip,
        declared_build=_first(_BUILD_CUES, text),
        export_month=_header_month(text),
        layout=("tsv" if delimiter == "\t" else "csv") + str(len(fields[0])),
        fingerprint=fingerprint(
            (row[0].strip(), row[1].strip(), row[2].strip()) for row in fields
        ),
    )


@dataclass(frozen=True)
class Revision:
    id: str
    vendor: str
    chip: str
    revision: str
    build: str
    markers: int
    ids_sha256: str
    sites_sha256: str
    sketch: tuple[int, ...]
    kind: str
    template: str | None
    template_relation: str | None
    template_added: int
    template_removed: int
    formats: str | None
    source: str
    exports: int
    period: str | None
    declares_vendor: bool

    @property
    def detectable(self) -> bool:
        return self.kind != "composite"


_KINDS = {"export", "reference-library", "composite"}
_RELATIONS = {"exact", "same-markers", "closest"}


def _revision(entry: dict[str, Any]) -> Revision:
    template = entry.get("template") or {}
    if not isinstance(template, dict):
        raise ValueError("template must be an object")
    revision = Revision(
        id=str(entry["id"]),
        vendor=str(entry["vendor"]),
        chip=str(entry["chip"]),
        revision=str(entry["revision"]),
        build=str(entry["build"]),
        markers=int(entry["markers"]),
        ids_sha256=str(entry["ids_sha256"]),
        sites_sha256=str(entry["sites_sha256"]),
        sketch=decode_sketch(str(entry["sketch"])),
        kind=str(entry["kind"]),
        template=template.get("name"),
        template_relation=template.get("relation"),
        template_added=int(template.get("added", 0)),
        template_removed=int(template.get("removed", 0)),
        formats=template.get("formats"),
        source=str(entry["source"]),
        exports=int(entry.get("exports", 0)),
        period=entry.get("period"),
        declares_vendor=bool(entry["declares_vendor"]),
    )
    if (
        revision.kind not in _KINDS
        or revision.build not in {"NCBI36", "GRCh37", "GRCh38"}
        or revision.markers <= 0
        or not re.fullmatch(r"[a-z0-9_]+", revision.id)
        or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (revision.ids_sha256, revision.sites_sha256)
        )
        or (revision.template and revision.template_relation not in _RELATIONS)
        or (revision.template and get_template_format(revision.template) is None)
    ):
        raise ValueError(f"invalid revision {revision.id!r}")
    return revision


@functools.cache
def load_catalogue(path: Path = CATALOGUE) -> tuple[Revision, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != _FORMAT or data.get("version") != 1:
            raise ValueError("unsupported catalogue format")
        revisions = tuple(_revision(entry) for entry in data["revisions"])
        if len({r.id for r in revisions}) != len(revisions):
            raise ValueError("duplicate revision IDs")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise WGSExtractError(
            f"Cannot read the packaged chip-revision catalogue: {error}"
        ) from error
    return revisions


@dataclass(frozen=True)
class Candidate:
    revision: Revision
    score: float
    exact_score: bool
    match: str


@dataclass(frozen=True)
class Detection:
    export: RawExport
    candidates: tuple[Candidate, ...]
    ambiguous: bool

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def confidence(self) -> float:
        best = self.best
        if best is None:
            return 0.0
        if self.ambiguous:
            return round(best.score / 2, 4)
        return round(best.score, 4)


def template_ids(format_name: str, roots: Sequence[str]) -> frozenset[str] | None:
    """A template's marker IDs, or None when it is not installed."""
    info = get_template_format(format_name)
    if info is None:
        return None
    try:
        _, bodies = _resolve_templates_for_format(list(roots), format_name, info)
    except WGSExtractError:
        return None
    return frozenset(
        marker for body in bodies for marker, _, _ in template_rows(format_name, body)
    )


def _margin(candidate: Candidate) -> float:
    if candidate.exact_score:
        return _EXACT_MARGIN
    # Two standard errors of the bottom-k estimate.
    p = candidate.score
    return max(_EXACT_MARGIN, 2 * math.sqrt(max(p * (1 - p), 0.01) / SKETCH_SIZE))


def detect(
    export: RawExport,
    catalogue: Sequence[Revision] | None = None,
    template_roots: Sequence[str] = (),
    *,
    limit: int = 3,
) -> Detection:
    """Rank catalogue revisions by marker-set similarity to an export.

    An identical marker multiset scores 1. Otherwise the score is the Jaccard
    similarity of the marker-ID sets: exact when the revision's template is
    installed and identical to it, otherwise a bottom-k estimate.
    """
    revisions = [r for r in catalogue or load_catalogue() if r.detectable]
    # A header naming the vendor selects its revisions; a file without one
    # cannot come from a vendor whose exports always name themselves.
    revisions = [
        r
        for r in revisions
        if (r.vendor == export.vendor if export.vendor else not r.declares_vendor)
    ] or revisions
    fp = export.fingerprint
    scored: list[Candidate] = []
    for revision in revisions:
        if revision.ids_sha256 == fp.ids_sha256:
            match = "exact" if revision.sites_sha256 == fp.sites_sha256 else "markers"
            scored.append(Candidate(revision, 1.0, True, match))
        else:
            # Different ID sets cannot reach the size ratio, nor 1.
            small, large = sorted((len(fp.ids), revision.markers))
            bound = min(small / large, (large - 1) / (large + 1))
            estimate = min(estimate_jaccard(fp.sketch, revision.sketch), bound)
            scored.append(Candidate(revision, estimate, False, "closest"))
    scored.sort(key=lambda c: (-c.score, not c.exact_score, c.revision.id))
    if scored and not scored[0].exact_score and template_roots:
        refined: list[Candidate] = []
        cutoff = scored[0].score - 2 * _margin(scored[0])
        for candidate in scored:
            revision = candidate.revision
            ids = (
                template_ids(revision.template, template_roots)
                if candidate.score >= cutoff
                and revision.template
                and revision.template_relation in {"exact", "same-markers"}
                else None
            )
            if ids is None:
                refined.append(candidate)
                continue
            score = len(fp.ids & ids) / len(fp.ids | ids)
            refined.append(Candidate(revision, score, True, "closest"))
        scored = sorted(
            refined, key=lambda c: (-c.score, not c.exact_score, c.revision.id)
        )
    ambiguous = (
        len(scored) > 1
        and scored[0].match != "exact"
        and scored[0].score - scored[1].score < _margin(scored[0])
    )
    return Detection(export, tuple(scored[:limit]), ambiguous)
