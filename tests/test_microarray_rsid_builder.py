import bz2
import gzip
import json
import sqlite3
from contextlib import closing

from scripts import build_microarray_rsid_map
from scripts.build_microarray_rsid_map import template_ids, write_subset


def test_archive_graph_reads_utf8_and_preserves_merge_and_withdrawal_states(
    tmp_path, monkeypatch
):
    snapshots = {
        "merged": [
            {"refsnp_id": 1, "merged_snapshot_data": {"merged_into": [2]}},
            {"refsnp_id": 2, "merged_snapshot_data": {"merged_into": [3]}},
        ],
        "withdrawn": [
            {"refsnp_id": 4, "withdrawn_snapshot_data": {}, "unused_metadata": "é"}
        ],
    }
    for kind, records in snapshots.items():
        with bz2.open(
            tmp_path / f"refsnp-{kind}.json.bz2", "wt", encoding="utf-8"
        ) as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        monkeypatch.setitem(
            build_microarray_rsid_map.SOURCES, kind, ("unused", len(records))
        )
    database = tmp_path / "graph.sqlite"
    build_microarray_rsid_map.build_graph(tmp_path, database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT * FROM identity ORDER BY rsid"
        ).fetchall() == [(1, "2"), (2, "3"), (4, None)]


def test_public_template_census_handles_csv_api_order_and_gzip(tmp_path):
    body = tmp_path / "body"
    body.mkdir()
    (body / "api.txt").write_text("1\t1000\trs10\nrs20\tX\t2000\n")
    with gzip.open(body / "vendor.csv.gz", "wt") as output:
        output.write('"rs30","1","3000"\n"i123","2","4000"\n')
    assert template_ids([tmp_path]) == {10, 20, 30}


def test_transitive_history_converges_or_fails_closed_and_is_reproducible(tmp_path):
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE identity(rsid INTEGER PRIMARY KEY, targets TEXT)"
        )
        connection.executemany(
            "INSERT INTO identity VALUES (?, ?)",
            [
                (1, "2"),
                (2, "3"),
                (4, None),
                (5, "4"),
                (6, "2,7"),
                (7, "3"),
                (8, "3,9"),
                (10, "11"),
                (11, "10"),
                (12, ""),
                (13, None),
            ],
        )
        relevant = {1, 5, 6, 8, 10, 12, 100}
        first, second = tmp_path / "first.gz", tmp_path / "second.gz"
        summary = write_subset(connection, relevant, first)
        write_subset(connection, relevant, second)
        assert first.read_bytes() == second.read_bytes()
        with gzip.open(first, "rt") as source:
            rows = {
                fields[0]: fields[1:]
                for line in source
                if not line.startswith("#")
                if (fields := line.rstrip("\n").split("\t"))
            }
        assert rows["rs1"] == rows["rs6"] == ["rs3", "merged"]
        assert rows["rs4"] == rows["rs5"] == [".", "withdrawn"]
        assert rows["rs8"] == [".", "ambiguous"]
        assert rows["rs10"] == rows["rs11"] == rows["rs12"] == [".", "unresolved"]
        assert "rs13" not in rows and "rs100" not in rows
        assert summary["history_rows"] == 10
    finally:
        connection.close()
