"""Read-only lookups the server answers itself, over HTTP (``GET /v1/<name>``)
and MCP (tools of the same name): a result manifest, rows of a ``vcf table``
output, and sections of a ``report``. They read files in the allowed folders
and change nothing, so MCP offers them without ``--allow-jobs``.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sqlite3
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core import personal_report
from wgsextract_cli.core.results import MANIFEST_SUFFIX
from wgsextract_cli.core.results import SCHEMA_ID as RESULT_SCHEMA
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_table import COLUMN_NAMES, COLUMNS, INTEGER, REAL

from .operations import PathGuard, RequestError

MAX_ROWS = 1000
_FAR = 2**62
CONFIDENCE_RANK = {"low": 0, "moderate": 1, "high": 2}
_TYPES = {c.name: c.type for c in COLUMNS}
_REGION = re.compile(
    r"(?P<chrom>[^:\s]+)(?::(?P<start>\d[\d,]*)(?:-(?P<end>\d[\d,]*))?)?"
)
_RSID = re.compile(r"rs\d+")
_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")
_SIGNIFICANCE_SPLIT = re.compile(r"[/|,]")


@dataclass(frozen=True)
class Query:
    """A lookup: MCP tool ``name``, HTTP ``GET /v1/<route>``."""

    name: str
    route: str
    summary: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    answer: Callable[[PathGuard, Mapping[str, Any]], dict[str, Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": False,
        }

    def run(self, guard: PathGuard, arguments: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(arguments) - set(self.properties)
        if unknown:
            raise RequestError(
                400, "usage", f"Unknown parameter(s): {', '.join(sorted(unknown))}."
            )
        for name in self.required:
            if arguments.get(name) in (None, ""):
                raise RequestError(400, "usage", f"{name} is required.")
        for name, value in arguments.items():
            _check_type(name, value, self.properties[name])
        return self.answer(guard, arguments)

    def from_query_string(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        """HTTP query parameters as JSON values of this query's schema."""
        arguments: dict[str, Any] = {}
        for name, values in query.items():
            spec = self.properties.get(name)
            value = values[-1]
            if spec is not None and spec["type"] == "array":
                # Comma-separated, repeated, or both.
                arguments[name] = [v for text in values for v in text.split(",") if v]
            elif spec is None:
                arguments[name] = value
            elif spec["type"] == "integer":
                try:
                    arguments[name] = int(value)
                except ValueError:
                    raise RequestError(
                        400, "usage", f"{name} must be an integer."
                    ) from None
            elif spec["type"] == "boolean":
                text = value.lower()
                if text not in _TRUE + _FALSE:
                    raise RequestError(400, "usage", f"{name} must be true or false.")
                arguments[name] = text in _TRUE
            else:
                arguments[name] = value
        return arguments


def _check_type(name: str, value: Any, spec: Mapping[str, Any]) -> None:
    kind = spec["type"]
    ok = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list) and all(isinstance(v, str) for v in value),
    }[kind]
    if not ok:
        raise RequestError(
            400, "usage", f"{name} must be a{'n' * (kind[0] in 'ai')} {kind}."
        )
    if "enum" in spec and value not in spec["enum"]:
        raise RequestError(
            400, "usage", f"{name} must be one of {', '.join(spec['enum'])}."
        )
    if kind == "integer" and not spec.get("minimum", value) <= value <= spec.get(
        "maximum", value
    ):
        raise RequestError(
            400,
            "usage",
            f"{name} must be {spec['minimum']} to {spec['maximum']}.",
        )
    if kind == "array" and "items" in spec and "enum" in spec["items"]:
        bad = [v for v in value if v not in spec["items"]["enum"]]
        if bad:
            raise RequestError(400, "usage", f"Unknown {name}: {', '.join(bad)}.")


def _path(guard: PathGuard, arguments: Mapping[str, Any]) -> Path:
    text = str(arguments["path"])
    guard.check_path("path", text)
    return Path(os.path.expanduser(text))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RequestError(404, "unreadable", f"Can't read {path}: {e}") from None


# Manifests -------------------------------------------------------------------


def read_manifest(guard: PathGuard, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _path(guard, arguments)
    if not path.name.endswith(MANIFEST_SUFFIX):
        raise RequestError(
            400, "usage", f"path must be a result manifest (*{MANIFEST_SUFFIX})."
        )
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("schema") != RESULT_SCHEMA:
        raise RequestError(400, "usage", f"{path} is not a {RESULT_SCHEMA} manifest.")
    if not arguments.get("full"):
        document.pop("provenance", None)
    return document


# Variants --------------------------------------------------------------------


def _region(text: str) -> tuple[str, int, int]:
    match = _REGION.fullmatch(text.strip())
    if not match:
        raise RequestError(400, "usage", f"region {text!r} isn't chrom[:start[-end]].")
    # Beyond any chromosome, and within SQLite's integers.
    start = min(int((match["start"] or "1").replace(",", "")), _FAR)
    end = min(int((match["end"] or str(_FAR)).replace(",", "")), _FAR)
    if match["start"] and not match["end"]:
        end = start
    if start > end:
        raise RequestError(400, "usage", f"region {text!r} ends before it starts.")
    return match["chrom"], start, end


def _typed(name: str, text: str) -> Any:
    if text == "":
        return None
    kind = _TYPES.get(name)
    try:
        if kind == INTEGER:
            return int(text)
        if kind == REAL:
            return float(text)
    except ValueError:
        return text
    return text


def _tsv_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    stream = (
        gzip.open(path, "rt", encoding="utf-8", newline="")
        if path.name.lower().endswith(".gz")
        else open(path, encoding="utf-8", newline="")  # noqa: SIM115
    )
    with stream:
        reader = csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader, [])
        for number, values in enumerate(reader, 1):
            yield (
                number,
                {k: _typed(k, v) for k, v in zip(header, values, strict=False)},
            )


def _parquet_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    from wgsextract_cli.core.table_writer import require_pyarrow

    try:
        pq = require_pyarrow()
    except WGSExtractError as e:
        raise RequestError(400, "usage", str(e)) from None
    number = 0
    for batch in pq.ParquetFile(path).iter_batches():
        for row in batch.to_pylist():
            number += 1
            yield number, row


def _json_value(value: Any) -> Any:
    """SQLite's typing is per value: a BLOB (or anything else that isn't
    JSON) in any column is returned as text."""
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _sqlite_rows(
    path: Path, after: int, where: list[str], values: list[Any]
) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise RequestError(400, "usage", f"Can't open {path}: {e}") from None
    try:
        db.row_factory = sqlite3.Row
        sql = " AND ".join(["rowid > ?", *where])
        for row in db.execute(
            f"SELECT rowid, * FROM variants WHERE {sql} ORDER BY rowid",  # noqa: S608
            [after, *values],
        ):
            values_ = {k: _json_value(v) for k, v in dict(row).items()}
            yield values_.pop("rowid"), values_
    except sqlite3.Error as e:
        raise RequestError(
            400, "usage", f"{path} isn't a variant database: {e}"
        ) from None
    finally:
        db.close()


def query_variants(guard: PathGuard, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _path(guard, arguments)
    if not path.is_file():
        raise RequestError(404, "not_found", f"No variant table {path}.")
    gene = arguments.get("gene")
    rsid = arguments.get("rsid")
    if rsid is not None and not _RSID.fullmatch(rsid):
        raise RequestError(400, "usage", f"rsid {rsid!r} isn't an rsID (rs123).")
    region = _region(arguments["region"]) if arguments.get("region") else None
    clinvar = str(arguments.get("clinvar") or "").lower()
    least = CONFIDENCE_RANK.get(str(arguments.get("min_confidence") or ""), -1)
    limit = int(arguments.get("limit") or 50)
    cursor = str(arguments.get("cursor") or "0")
    # ASCII digits only (str.isdigit accepts "²"), within SQLite's integers.
    if not re.fullmatch(r"[0-9]+", cursor) or int(cursor) > _FAR:
        raise RequestError(400, "usage", "cursor must be a next_cursor value.")
    after = int(cursor)

    def keep(row: Mapping[str, Any]) -> bool:
        if gene is not None and row.get("gene") != gene:
            return False
        if rsid is not None and rsid not in str(row.get("rsid") or "").split(";"):
            return False
        if region is not None:
            chrom, start, end = region
            pos = row.get("pos")
            if row.get("chrom") != chrom or not isinstance(pos, int):
                return False
            if not start <= pos <= end:
                return False
        if clinvar:
            terms = _SIGNIFICANCE_SPLIT.split(
                str(row.get("clinvar_significance") or "")
            )
            if clinvar not in (t.lower() for t in terms):
                return False
        rank = CONFIDENCE_RANK.get(str(row.get("confidence") or ""), -1)
        return least < 0 or rank >= least

    name = path.name.lower()
    if name.endswith(".sqlite"):
        # Narrow with the database's indexes; keep() decides. An rsID can be
        # one of several in a cell (rs1;rs2), so that filter scans the table.
        where: list[str] = []
        values: list[Any] = []
        if gene is not None:
            where.append("gene = ?")
            values.append(gene)
        if rsid is not None:
            where.append("rsid LIKE ?")
            values.append(f"%{rsid}%")
        if region is not None:
            where.append("chrom = ? AND pos BETWEEN ? AND ?")
            values.extend(region)
        rows = _sqlite_rows(path, after, where, values)
    elif name.endswith(".parquet"):
        rows = _parquet_rows(path)
    elif name.endswith((".tsv", ".tsv.gz")):
        rows = _tsv_rows(path)
    else:
        raise RequestError(
            400,
            "usage",
            "path must be a vcf table output: *_variants.tsv, .parquet or .sqlite.",
        )
    wanted = arguments.get("columns") or COLUMN_NAMES
    found: list[dict[str, Any]] = []
    next_cursor = None
    last = after
    try:
        for number, row in rows:
            if number <= after or not keep(row):
                continue
            if len(found) == limit:
                next_cursor = str(last)
                break
            found.append({k: row[k] for k in wanted if row.get(k) not in (None, "")})
            last = number
    except (OSError, EOFError, ValueError, csv.Error, zlib.error) as e:
        raise RequestError(400, "usage", f"Can't read {path}: {e}") from None
    finally:
        close = getattr(rows, "close", None)
        if close:
            close()
    return {"path": str(path), "rows": found, "next_cursor": next_cursor}


# Report ----------------------------------------------------------------------

_REPORT_SUFFIX = "_personal_report.json"


def _report_file(guard: PathGuard, path: Path) -> Path:
    if path.is_dir():
        # A dangling link, or a report deleted since the glob, is skipped.
        dated = []
        for candidate in path.glob(f"*{_REPORT_SUFFIX}"):
            try:
                dated.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        found = [p for _, p in sorted(dated)]
        if not found:
            raise RequestError(
                404,
                "no_report",
                f"{path} has no report; run the report operation on it first.",
            )
        path = found[-1]
        guard.check_path("path", path)
    return path


def report_section(guard: PathGuard, arguments: Mapping[str, Any]) -> dict[str, Any]:
    path = _report_file(guard, _path(guard, arguments))
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("schema") != (
        personal_report.SCHEMA_ID
    ):
        raise RequestError(
            400, "usage", f"{path} is not a {personal_report.SCHEMA_ID} report."
        )
    sections = document.get("sections") or {}
    if not isinstance(sections, dict) or not all(
        isinstance(s, dict) for s in sections.values()
    ):
        raise RequestError(400, "usage", f"{path} has malformed report sections.")
    head = {
        "path": str(path),
        "generated": document.get("generated"),
        "sample": document.get("sample"),
        "disclaimer": document.get("disclaimer"),
    }
    name = arguments.get("section")
    if not name:
        return {
            **head,
            "sections": {
                key: {"status": s.get("status"), "summary": s.get("summary")}
                for key, s in sections.items()
            },
        }
    if name not in sections:
        raise RequestError(
            404,
            "no_section",
            f"No section {name!r}; the report has {', '.join(sections)}.",
        )
    return {**head, "section": name, **sections[name]}


_PATH = {"type": "string", "description": "Absolute path in an allowed folder."}
QUERIES: dict[str, Query] = {
    q.name: q
    for q in (
        Query(
            "read_manifest",
            "manifest",
            "Read a result manifest (*.manifest.json) a WGS Extract command "
            "wrote: its outputs, metrics, warnings and details.",
            {
                "path": _PATH,
                "full": {
                    "type": "boolean",
                    "description": "Include provenance (tools, params).",
                },
            },
            ("path",),
            read_manifest,
        ),
        Query(
            "query_variants",
            "variants",
            "Look up a sample's variants in a `vcf table` output (*_variants "
            ".sqlite, .tsv or .parquet) by gene, rsID, region, ClinVar "
            "significance or confidence. Returns at most `limit` rows and a "
            "next_cursor to continue.",
            {
                "path": _PATH,
                "gene": {"type": "string", "description": "Gene symbol (BRCA1)."},
                "rsid": {"type": "string", "description": "dbSNP rsID (rs429358)."},
                "region": {
                    "type": "string",
                    "description": "chrom, chrom:pos or chrom:start-end, named "
                    "as in the table.",
                },
                "clinvar": {
                    "type": "string",
                    "description": "A ClinVar significance term, e.g. Pathogenic "
                    "or Likely_pathogenic (case-insensitive).",
                },
                "min_confidence": {
                    "type": "string",
                    "enum": list(CONFIDENCE_RANK),
                    "description": "Only calls labelled at least this confident.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(COLUMN_NAMES)},
                    "description": "Columns to return (default: all non-empty).",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
                "cursor": {
                    "type": "string",
                    "description": "next_cursor from the previous page.",
                },
            },
            ("path",),
            query_variants,
        ),
        Query(
            "report_section",
            "report",
            "Read a sample's personal report (from the report operation): each "
            "section's status and summary, or one section in full.",
            {
                "path": {
                    "type": "string",
                    "description": "The sample folder, or its "
                    "*_personal_report.json, in an allowed folder.",
                },
                "section": {
                    "type": "string",
                    "enum": list(personal_report.SECTIONS),
                    "description": "Return this section in full.",
                },
            },
            ("path",),
            report_section,
        ),
    )
}
