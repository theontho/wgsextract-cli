"""`vcf table`: a VCF's variants and annotations as a flat TSV and SQLite table."""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from wgsextract_cli import __version__
from wgsextract_cli.core.atomic_output import atomic_output
from wgsextract_cli.core.builds import build_from_chr1_length, build_from_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_models import sequence_lengths
from wgsextract_cli.core.results import (
    current_result,
    human_output,
    redact_argv,
    warn,
)
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    popen,
    run_command,
)
from wgsextract_cli.core.variant_files import ensure_vcf_prepared, get_vcf_samples
from wgsextract_cli.core.variant_table import (
    COLUMN_NAMES,
    COLUMNS,
    Row,
    VcfHeader,
    parse_header,
    variant_rows,
)

from .basic import require_vcf_input
from .pharmcat import summarize

_VCF_SUFFIX = re.compile(r"\.(g?vcf(\.b?gz)?|bcf)$", re.IGNORECASE)
_BATCH = 50_000
TABLE = "variants"
# Indexes for the lookups agents make most: a gene, an rsID, a position.
INDEXES = {"gene": ("gene",), "rsid": ("rsid",), "position": ("chrom", "pos")}


def table_stem(vcf: str) -> str:
    return _VCF_SUFFIX.sub("", Path(vcf).name) + "_variants"


def _sha256(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _reference_build(header: VcfHeader) -> str | None:
    build = build_from_chr1_length(sequence_lengths("\n".join(header.lines)))
    if build:
        return build
    for line in header.lines:
        if line.startswith("##reference="):
            return build_from_path(line.partition("=")[2])
    return None


def _tsv_value(value: str | int | float | None) -> str:
    return "" if value is None else str(value)


def _create_sqlite(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA journal_mode = OFF")
    db.execute("PRAGMA synchronous = OFF")
    columns = ", ".join(f"{c.name} {c.type}" for c in COLUMNS)
    db.execute(f"CREATE TABLE {TABLE} ({columns})")
    db.execute("CREATE TABLE columns (name TEXT, type TEXT, description TEXT)")
    db.executemany(
        "INSERT INTO columns VALUES (?, ?, ?)",
        [(c.name, c.type, c.description) for c in COLUMNS],
    )
    db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")


def _finish_sqlite(db: sqlite3.Connection, metadata: dict[str, str]) -> None:
    for name, columns in INDEXES.items():
        db.execute(f"CREATE INDEX {TABLE}_{name} ON {TABLE} ({', '.join(columns)})")
    db.executemany("INSERT INTO metadata VALUES (?, ?)", sorted(metadata.items()))
    db.commit()
    db.execute("ANALYZE")
    db.commit()


def _batches(rows: Iterable[Row]) -> Iterator[list[Row]]:
    batch: list[Row] = []
    for row in rows:
        batch.append(row)
        if len(batch) == _BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def _pharmcat_calls(path: str | None) -> dict[str, tuple[str, str]]:
    if not path:
        return {}
    try:
        calls = summarize(path)
    except (OSError, ValueError) as e:
        raise WGSExtractError(f"Could not read PharmCAT results {path}: {e}") from e
    by_gene: dict[str, tuple[str, str]] = {}
    for gene, diplotype, phenotype in calls:
        if gene in by_gene:
            old = by_gene[gene]
            diplotype, phenotype = f"{old[0]}; {diplotype}", f"{old[1]}; {phenotype}"
        by_gene[gene] = (diplotype, phenotype)
    return by_gene


def _select_sample(vcf: str, requested: str | None) -> tuple[list[str], str | None]:
    samples = get_vcf_samples(vcf)
    if requested:
        if requested not in samples:
            raise UsageError(
                f"Sample '{requested}' is not in {vcf}; it has: "
                f"{', '.join(samples) or 'no samples'}."
            )
        return samples, requested
    if len(samples) > 1:
        logging.info(
            f"{vcf} has {len(samples)} samples; using the first, {samples[0]}. "
            "Pick another with --sample."
        )
    return samples, (samples[0] if samples else None)


class _VcfStream:
    """``bcftools view`` of the input as text: its parsed header and data lines.

    stderr is drained by a thread, so a chatty bcftools can't stall stdout.
    """

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.proc = popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self.stdout = self.proc.stdout
        self._stderr: deque[str] = deque(maxlen=20)
        stderr = self.proc.stderr
        self._drain = threading.Thread(
            target=lambda: self._stderr.extend(stderr), daemon=True
        )
        self._drain.start()
        lines = []
        for line in self.stdout:
            lines.append(line)
            if line.startswith("#CHROM"):
                break
        else:
            self.check()  # no #CHROM line: bcftools failed or this isn't a VCF
            raise WGSExtractError(f"{command[-1]} has no #CHROM header line.")
        self.header = parse_header(lines)

    def check(self) -> None:
        if self.proc.wait() != 0:
            self._drain.join(timeout=5)
            detail = "".join(self._stderr).strip()
            raise WGSExtractError(
                f"bcftools failed reading {self.command[-1]}: {detail}"
            )

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.stdout.close()


def _write_tables(
    stream: _VcfStream,
    rows: Iterable[Row],
    tsv_path: str,
    sqlite_path: str | None,
    metadata: dict[str, str],
) -> int:
    """Stream ``rows`` into the TSV (and SQLite database); return the count.
    Each file appears under its final name only once complete."""
    count = 0
    with ExitStack() as stack:
        tsv_stage = stack.enter_context(atomic_output(tsv_path))
        tsv = stack.enter_context(open(tsv_stage, "w", newline="\n", encoding="utf-8"))
        tsv.write("\t".join(COLUMN_NAMES) + "\n")
        db = None
        if sqlite_path:
            db = sqlite3.connect(stack.enter_context(atomic_output(sqlite_path)))
            stack.callback(db.close)
            _create_sqlite(db)
        insert = f"INSERT INTO {TABLE} VALUES ({', '.join('?' * len(COLUMNS))})"
        for batch in _batches(rows):
            tsv.writelines("\t".join(map(_tsv_value, row)) + "\n" for row in batch)
            if db is not None:
                db.executemany(insert, batch)
            count += len(batch)
        stream.check()
        if db is not None:
            _finish_sqlite(db, {**metadata, "rows": str(count)})
    return count


def _indexed_input(path: str) -> str:
    """``path`` with a current index, for a region query. A BCF is indexed
    in place with ``bcftools index`` (CSI); text VCFs go through the usual
    bgzip + tabix preparation."""
    if not path.lower().endswith(".bcf"):
        return ensure_vcf_prepared(path)
    index = Path(path + ".csi")
    if not index.exists() or index.stat().st_mtime < Path(path).stat().st_mtime:
        logging.info(f"Indexing {path}...")
        run_command(["bcftools", "index", "-f", path])
    return path


def cmd_table(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools"])
    input_vcf = require_vcf_input(args)
    if not Path(input_vcf).is_file():
        raise UsageError(f"VCF not found: {input_vcf}")
    if args.region:
        input_vcf = _indexed_input(input_vcf)  # -r needs an index
    outdir = str(absolute_path(args.outdir or str(Path(input_vcf).parent)))
    Path(outdir).mkdir(parents=True, exist_ok=True)
    samples, sample = _select_sample(input_vcf, args.sample)
    pharmcat = _pharmcat_calls(args.pharmcat_json)
    result = current_result()
    if args.pharmcat_json:
        result.add_input(args.pharmcat_json, "pharmcat_phenotype")

    stem = str(Path(outdir, table_stem(input_vcf)))
    tsv_path = stem + ".tsv"
    sqlite_path = None if args.no_sqlite else stem + ".sqlite"

    command = ["bcftools", "view", "--no-version"]
    if sample and len(samples) > 1:
        command += ["-s", sample, "-I"]  # -I: keep INFO as it is
    if args.region:
        command += ["-r", args.region]
    command.append(input_vcf)

    logging.info(f"Writing the variant table from {input_vcf}...")
    stream = _VcfStream(command)
    try:
        header = stream.header
        build = _reference_build(header)
        annotations = ",".join(header.annotations)
        metadata = {
            "wgsextract_version": __version__,
            "created": datetime.now(timezone.utc).isoformat(),
            "source_vcf": str(absolute_path(input_vcf)),
            "sample": sample or "",
            "reference_build": build or "",
            "region": args.region or "",
            "annotations": annotations,
            "carried_only": str(not args.all_alleles).lower(),
            "command_line": " ".join(redact_argv(sys.argv)),
        }
        if sqlite_path:
            metadata["source_sha256"] = _sha256(input_vcf) or ""
        rows = _write_tables(
            stream,
            variant_rows(
                stream.stdout,
                header,
                sample_index=0 if sample else None,
                carried_only=not args.all_alleles,
                pharmcat=pharmcat,
            ),
            tsv_path,
            sqlite_path,
            metadata,
        )
    finally:
        stream.close()

    result.add_output(tsv_path, "variant_table", kind="tsv", metrics={"rows": rows})
    if sqlite_path:
        result.add_output(
            sqlite_path, "variant_database", kind="sqlite", metrics={"rows": rows}
        )
    result.add_metrics(
        rows=rows, sample=sample, reference_build=build, annotations=annotations
    )
    if not header.annotations:
        warn(
            "nothing_annotated",
            f"{input_vcf} has no VEP, SnpEff, ClinVar, gnomAD or PharmGKB "
            "annotations, so those columns are empty. Annotate it first, e.g. "
            "with 'wgsextract vep' and 'wgsextract vcf clinvar'.",
            path=input_vcf,
        )
    if rows == 0:
        warn("no_variants", f"No variants to tabulate in {input_vcf}.", path=input_vcf)
    found = f" ({annotations})" if annotations else ""
    human_output(f"{rows:,} variant rows{found}:")
    for path in filter(None, (tsv_path, sqlite_path)):
        human_output(f"  {path}")
