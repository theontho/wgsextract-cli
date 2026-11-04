import csv
import gzip
import sys

import pytest

from wgsextract_cli.core.table_writer import (
    Column,
    TableWriter,
    check_format,
    provenance,
    write_table,
)
from wgsextract_cli.core.utils import MissingDependencyError

COLUMNS = (
    Column("chrom", "TEXT", "Contig."),
    Column("pos", "INTEGER", "Position."),
    Column("af", "REAL", "Allele frequency."),
    Column("pct", "REAL", "Percent.", ".2f"),
    Column("pass", "BOOLEAN", "Passed."),
)
ROWS = [
    ("chr1", 10, 0.5, 12.345, True),
    ("chr2", None, 1e-06, 0.0, False),
    ("", 30, None, None, None),
    ("chrX", 2**40, 0.125, 100.0, True),
    ("chrM", 1, 0.0, 99.999, False),
]


@pytest.fixture
def no_pyarrow(monkeypatch):
    for name in ("pyarrow", "pyarrow.parquet"):
        monkeypatch.setitem(sys.modules, name, None)


def test_tsv_and_parquet_hold_the_same_typed_rows(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    metadata = provenance([__file__], reference_build="hg38", sorted_by="chrom, pos")
    tsv, parquet = tmp_path / "t.tsv", tmp_path / "t.parquet"
    assert write_table(tsv, COLUMNS, ROWS, "tsv") == len(ROWS)
    with TableWriter(
        parquet, COLUMNS, "parquet", metadata=metadata, row_group_rows=2
    ) as writer:
        writer.write(iter(ROWS))
    assert writer.rows == len(ROWS)

    with open(tsv, newline="") as f:
        text_rows = list(csv.reader(f, delimiter="\t"))
    assert text_rows[0] == [c.name for c in COLUMNS]
    assert text_rows[2] == ["chr2", "", "1e-06", "0.00", "false"]

    table = pq.read_table(parquet)
    assert [str(t) for t in table.schema.types] == [
        "string", "int64", "double", "double", "bool"
    ]  # fmt: skip
    parquet_rows = [tuple(r.values()) for r in table.to_pylist()]
    assert parquet_rows == ROWS
    # The TSV is the Parquet's values in their text form.
    assert text_rows[1:] == [
        [c.text(v) for c, v in zip(COLUMNS, row, strict=True)] for row in parquet_rows
    ]

    footer = pq.ParquetFile(parquet)
    assert footer.metadata.num_row_groups == 3
    assert footer.metadata.row_group(0).column(0).compression == "ZSTD"
    stored = {k.decode(): v.decode() for k, v in footer.schema_arrow.metadata.items()}
    assert stored["reference_build"] == "hg38"
    assert stored["sorted_by"] == "chrom, pos"
    assert stored["input_fingerprint"].startswith("wgse-fp1:")
    assert footer.schema_arrow.field("pos").metadata == {b"description": b"Position."}


def test_failed_write_leaves_no_table(tmp_path):
    def rows():
        yield ROWS[0]
        raise RuntimeError("input broke")

    path = tmp_path / "t.tsv"
    with pytest.raises(RuntimeError):
        write_table(path, COLUMNS, rows(), "tsv")
    assert list(tmp_path.iterdir()) == []


def test_missing_pyarrow_is_a_clear_error_before_any_file(tmp_path, no_pyarrow):
    with pytest.raises(MissingDependencyError, match=r"wgsextract-cli\[parquet\]"):
        check_format("parquet")
    check_format("tsv")
    check_format(None)
    with pytest.raises(MissingDependencyError, match="--format tsv"):
        write_table(tmp_path / "t.parquet", COLUMNS, ROWS, "parquet")
    assert list(tmp_path.iterdir()) == []


def test_bedmethyl_parquet_streams_modkit_output(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    from wgsextract_cli.commands import methylation

    bed = tmp_path / "x.bedmethyl.gz"
    lines = [
        f"chr1\t{i}\t{i + 1}\tm\t10\t.\t{i}\t{i + 1}\t255,0,0\t10\t40.00\t4\t6"
        "\t0\t0\t0\t0\t0\n"
        for i in range(5)
    ]
    # bgzip output is several gzip members back to back.
    with open(bed, "wb") as out:
        for line in lines:
            out.write(gzip.compress(line.encode()))
    path = tmp_path / "x.bedmethyl.parquet"
    assert methylation.bedmethyl_to_parquet(str(bed), str(path), {"k": "v"}) == 5
    table = pq.read_table(path)
    assert table.column_names == [c.name for c in methylation.BEDMETHYL_TABLE]
    assert table.slice(4).to_pylist() == [
        {
            "chrom": "chr1", "start": 4, "end": 5, "mod_code": "m", "score": 10,
            "strand": ".", "n_valid_cov": 10, "percent_modified": 40.0, "n_mod": 4,
            "n_canonical": 6, "n_other_mod": 0, "n_delete": 0, "n_fail": 0,
            "n_diff": 0, "n_nocall": 0,
        }
    ]  # fmt: skip
    assert pq.read_schema(path).metadata[b"k"] == b"v"


def test_empty_bedmethyl_gives_an_empty_typed_parquet(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    from wgsextract_cli.commands import methylation

    bed = tmp_path / "x.bedmethyl.gz"
    bed.write_bytes(gzip.compress(b""))
    path = tmp_path / "x.bedmethyl.parquet"
    assert methylation.bedmethyl_to_parquet(str(bed), str(path), {}) == 0
    table = pq.read_table(path)
    assert table.num_rows == 0
    assert table.schema.field("start").type == "int64"


def test_methylation_summary_tsv_is_unchanged(tmp_path):
    from wgsextract_cli.commands import methylation

    summary = methylation.ModSummary("5mC", 4, 3, 20, 36.666, 50, 100 / 3, 0)
    path = tmp_path / "s.tsv"
    methylation.write_summary(str(path), [summary])
    assert (
        path.read_text().splitlines()[1] == "5mC\t4\t3\t20\t36.67\t50.00\t33.33\t0.00"
    )


def test_microarray_genotype_table(tmp_path):
    from wgsextract_cli.commands import microarray
    from wgsextract_cli.core.results import CommandResult, collecting

    kit = tmp_path / "s_CombinedKit.txt"
    kit.write_text("# RSID\tCHROM\tPOS\tRESULT\nrs1\t1\t100\tAG\ni2\tX\t5\t--\n")
    path = tmp_path / "s_genotypes.tsv"
    result = CommandResult("microarray")
    with collecting(result):
        microarray._export_genotypes(str(kit), str(path), "tsv", {})
    assert path.read_text() == (
        "rsid\tchrom\tpos\tgenotype\nrs1\t1\t100\tAG\ni2\tX\t5\t--\n"
    )
    assert result.outputs[0].metrics == {"rows": 2}


def test_chm13_microarray_exports_its_grch37_genotype_table(tmp_path, monkeypatch):
    import argparse
    from types import SimpleNamespace

    from wgsextract_cli.commands import _microarray_chm13, microarray

    bam = tmp_path / "s.bam"
    bam.touch()
    fasta = tmp_path / "chm13.fa"
    fasta.touch()
    kit = tmp_path / "CHM13_x" / "s_CombinedKit.txt"
    kit.parent.mkdir()
    kit.write_text("# RSID\tCHROM\tPOS\tRESULT\nrs1\t1\t100\tAG\n")
    for name in ("calculate_bam_md5", "verify_dependencies", "log_dependency_info"):
        monkeypatch.setattr(microarray, name, lambda *_a, **_k: "")
    monkeypatch.setattr(microarray, "warn_low_depth", lambda *_a, **_k: None)
    monkeypatch.setattr(
        microarray,
        "ReferenceLibrary",
        lambda *_a, **_k: SimpleNamespace(
            root=str(tmp_path), build="T2T", fasta=str(fasta), ref_vcf_tab=None,
            ploidy_file=None, liftover_chain=None,
        ),
    )  # fmt: skip
    monkeypatch.setattr(_microarray_chm13, "run_chm13", lambda *_a, **_k: kit)
    microarray.run(
        argparse.Namespace(
            input=str(bam), ref=str(tmp_path), formats="23andme_v5", threads="1",
            outdir=str(tmp_path), ref_vcf_tab=None, ploidy_file=None, region=None,
            table_format="tsv",
        )
    )  # fmt: skip
    table = tmp_path / "s_genotypes.tsv"
    assert table.read_text() == "rsid\tchrom\tpos\tgenotype\nrs1\t1\t100\tAG\n"


def test_microarray_format_names_formats(capsys):
    from wgsextract_cli.cli.parser import build_parser

    with pytest.raises(SystemExit):
        build_parser()[0].parse_args(["microarray", "--format", "23andme_v5"])
    assert "pick vendor kits with --formats" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["info"], None),
        (["info", "--format", "parquet", "calculate-coverage"], "parquet"),
        (["info", "calculate-coverage", "--format", "tsv"], "tsv"),
        (["info", "coverage-sample"], None),
    ],
)
def test_info_format_is_read_before_or_after_the_subcommand(argv, expected):
    from wgsextract_cli.cli.parser import build_parser

    args = build_parser()[0].parse_args([*argv, "--input", "x.bam"])
    assert args.table_format == expected


def test_info_chromosome_rows_parse_the_breadth():
    from wgsextract_cli.commands.info.runner import _chromosome_rows

    table = (
        "Seq Name,Model Len,Model N Len,# Segs Map,Map Gbases,Map ARD,"
        "Breadth Coverage\n"
        "chr1,1000,10,50,0.01,30,98 %\n"
        "chr2,500,0,5,0.00,2,~7 %\n"
        "chrM,16569,1,9,0.00,40,\n"
    )
    assert _chromosome_rows(table) == [
        ("chr1", 1000, 10, 50, 0.01, 30, 98.0),
        ("chr2", 500, 0, 5, 0.0, 2, 7.0),
        ("chrM", 16569, 1, 9, 0.0, 40, None),
    ]


def test_info_qc_row_types_the_manifest_metrics(tmp_path):
    from wgsextract_cli.commands.info.runner import QC_TABLE, _qc_row

    row = _qc_row({"sex": "Male", "is_paired": True, "avg_read_len": 150, "x": 1})
    named = dict(zip([c.name for c in QC_TABLE], row, strict=True))
    assert (named["sex"], named["is_paired"], named["avg_read_len"]) == (
        "Male", True, 150.0,
    )  # fmt: skip
    assert named["alt_aware"] is None
    path = tmp_path / "qc.tsv"
    write_table(path, QC_TABLE, [row], "tsv")
    values = path.read_text().splitlines()[1].split("\t")
    assert values[:5] == ["", "", "Male", "150", "true"]


def test_arrow_batches_are_gathered_into_full_row_groups(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    columns = COLUMNS[:2]
    path = tmp_path / "t.parquet"
    with TableWriter(path, columns, "parquet", row_group_rows=4) as writer:
        for start in (0, 3, 6):
            # Column names come from the writer, in order.
            writer.write_arrow(
                pa.record_batch(
                    [pa.array(["c"] * 3), pa.array(range(start, start + 3))],
                    names=["a", "b"],
                )
            )
    assert writer.rows == 9
    meta = pq.ParquetFile(path).metadata
    assert [meta.row_group(i).num_rows for i in range(meta.num_row_groups)] == [4, 4, 1]
    assert pq.read_table(path).column("pos").to_pylist() == list(range(9))
