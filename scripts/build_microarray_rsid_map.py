"""Build a compact public dbSNP identity history for installed vendor templates.

Developer-only: supply the pinned archives and optional reflib template roots.
The CLI never downloads these archives or builds this graph.
"""

from __future__ import annotations

import argparse
import bz2
import collections
import csv
import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

SOURCES = {
    "merged": (
        "3ea8d75ecd89d87c571784a74ba09099806ec817eff9dd015a6c3874048dafee",
        21585672,
    ),
    "withdrawn": (
        "ad50ed4877bab0a39209effb75585aa51c71a189d022427ca5784706cd671679",
        6854924,
    ),
}
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src/wgsextract_cli/assets/microarray/raw_file_templates"
)


def template_ids(roots: list[Path]) -> set[int]:
    result: set[int] = set()
    for root in roots:
        body = root / "body"
        if not body.is_dir():
            raise FileNotFoundError(body)
        for path in sorted(body.iterdir()):
            if not path.is_file():
                continue
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8-sig") as source:
                for line in source:
                    if not line.strip() or line.startswith("#"):
                        continue
                    fields = next(
                        csv.reader([line], delimiter="\t" if "\t" in line else ",")
                    )
                    for field in fields[:3]:
                        value = field.strip().strip('"')
                        if value.startswith("rs") and value[2:].isdigit():
                            result.add(int(value[2:]))
    if not result:
        raise ValueError("No numeric rsIDs in template bodies")
    return result


def verify_archives(directory: Path) -> None:
    for kind, (expected, _) in SOURCES.items():
        path = directory / f"refsnp-{kind}.json.bz2"
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"Unexpected dbSNP build157 archive checksum: {path.name}")


def build_graph(directory: Path, database: Path) -> None:
    if database.exists():
        raise FileExistsError(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-32768")
        connection.execute(
            "CREATE TABLE identity(rsid INTEGER PRIMARY KEY, targets TEXT)"
        )
        for kind, (_, expected_count) in SOURCES.items():
            count = 0
            batch = []
            with bz2.open(
                directory / f"refsnp-{kind}.json.bz2", "rt", encoding="utf-8"
            ) as source:
                for line in source:
                    record = json.loads(line)
                    targets = None
                    if kind == "merged":
                        targets = ",".join(
                            str(int(value))
                            for value in record["merged_snapshot_data"]["merged_into"]
                        )
                    elif "withdrawn_snapshot_data" not in record:
                        raise ValueError("Missing withdrawn snapshot")
                    batch.append((int(record["refsnp_id"]), targets))
                    count += 1
                    if len(batch) == 50000:
                        connection.executemany(
                            "INSERT INTO identity VALUES (?, ?)", batch
                        )
                        connection.commit()
                        batch.clear()
            connection.executemany("INSERT INTO identity VALUES (?, ?)", batch)
            connection.commit()
            if count != expected_count:
                raise ValueError(f"Unexpected {kind} record count: {count}")


def write_subset(
    connection: sqlite3.Connection, relevant: set[int], output: Path
) -> dict:
    """Include visited historical aliases as well as requested template IDs."""
    selected: dict[int, str | None] = {}
    cache: dict[int, tuple[str, int | None]] = {}

    def resolve(rsid: int, path: tuple[int, ...] = ()) -> tuple[str, int | None]:
        if rsid in path or len(path) >= 64:
            return "unresolved", None
        if rsid in cache:
            return cache[rsid]
        row = connection.execute(
            "SELECT targets FROM identity WHERE rsid=?", (rsid,)
        ).fetchone()
        if row is None:
            return "merged", rsid
        targets = row[0]
        selected[rsid] = targets
        result: tuple[str, int | None]
        if targets is None:
            result = ("withdrawn", None)
        elif not targets:
            result = ("unresolved", None)
        else:
            destinations = {
                resolve(int(target), (*path, rsid)) for target in targets.split(",")
            }
            result = (
                next(iter(destinations))
                if len(destinations) == 1
                else ("ambiguous", None)
            )
        cache[rsid] = result
        return result

    for rsid in sorted(relevant):
        resolve(rsid)
    counts: collections.Counter[str] = collections.Counter()
    with (
        output.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed,
    ):
        compressed.write(
            b"# NCBI dbSNP build157; public template IDs and visited historical aliases\n"
            b"# RSID\tCURRENT_RSID\tSTATUS\n"
        )
        for rsid in sorted(selected):
            status, current = resolve(rsid)
            counts[status] += 1
            compressed.write(
                f"rs{rsid}\t{'rs' + str(current) if current else '.'}\t{status}\n".encode()
            )
    return {
        "template_rsid_count": len(relevant),
        "history_rows": len(selected),
        "statuses": dict(counts),
        "gzip_bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--templates", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reuse-graph", type=Path, help="Reuse a previously verified local graph"
    )
    args = parser.parse_args()
    verify_archives(args.archives)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    database = args.reuse_graph or args.work_dir / "identity-graph.sqlite"
    if not args.reuse_graph:
        build_graph(args.archives, database)
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.execute("PRAGMA temp_store=MEMORY")
        count, withdrawn = connection.execute(
            "SELECT count(*), sum(targets IS NULL) FROM identity"
        ).fetchone()
        if (
            count != sum(value[1] for value in SOURCES.values())
            or withdrawn != SOURCES["withdrawn"][1]
        ):
            raise ValueError("Incomplete identity graph")
        summary = write_subset(
            connection, template_ids([TEMPLATES, *args.templates]), args.output
        )
    (args.work_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
