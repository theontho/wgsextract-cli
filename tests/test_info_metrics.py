"""`info` on tiny real BAM/CRAM files: read length, insert size, mapped
reads, depth, sex and file state as samtools sees them, plus the edge cases
(empty, unsorted, unindexed, CRAM without its reference, numeric contigs)."""

import csv
import io
import json
import random
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli.commands.info import metrics, render, runner
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import calculate_bam_md5
from wgsextract_cli.main import main

LENGTHS = {"chr1": 1000, "chrX": 1000, "chrY": 1000, "chrM": 500}
QUAL = "I" * 50
NOVASEQ = "A00123:8:HABCDEFXX:2:1101:1000:{}"


def _pairs(contig: str, n: int, seq: str, start: int = 1) -> list[str]:
    """n 50 bp FR pairs with a 250 bp insert, named after the contig."""
    lines = []
    for i in range(n):
        pos = start + (i * 7) % (len(seq) - 300)
        name = NOVASEQ.format(1000 * list(LENGTHS).index(contig) + i)
        r1, r2 = seq[pos - 1 : pos + 49], seq[pos + 199 : pos + 249]
        lines += [
            f"{name}\t99\t{contig}\t{pos}\t60\t50M\t=\t{pos + 200}\t250\t{r1}\t{QUAL}",
            f"{name}\t147\t{contig}\t{pos + 200}\t60\t50M\t=\t{pos}\t-250\t{r2}\t{QUAL}",
        ]
    return lines


UNMAPPED = [
    f"{NOVASEQ.format(9999)}\t77\t*\t0\t0\t*\t*\t0\t0\t{'A' * 50}\t{QUAL}",
    f"{NOVASEQ.format(9999)}\t141\t*\t0\t0\t*\t*\t0\t0\t{'C' * 50}\t{QUAL}",
]


@pytest.fixture(scope="module")
def genome(tmp_path_factory):
    root = tmp_path_factory.mktemp("info")
    rng = random.Random(5)
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(n)) for c, n in LENGTHS.items()
    }
    ref = write_fasta(root / "ref.fa", seqs)
    return {"root": root, "ref": str(ref), "seqs": seqs}


def _sample(genome, path: Path, counts: dict[str, int], unmapped: bool = True,
            rename: dict[str, str] | None = None) -> str:  # fmt: skip
    """A sorted, indexed BAM with counts[contig] pairs on each contig."""
    rename = rename or {}
    header = [f"@SQ\tSN:{rename.get(c, c)}\tLN:{n}" for c, n in LENGTHS.items()]
    reads = []
    for contig, n in counts.items():
        for line in _pairs(contig, n, genome["seqs"][contig]):
            f = line.split("\t")
            f[2] = rename.get(f[2], f[2])
            reads.append("\t".join(f))
    return sam_to_bam(header + reads + (UNMAPPED if unmapped else []), path)


MALE = {"chr1": 20, "chrX": 10, "chrY": 4, "chrM": 1}
FEMALE = {"chr1": 20, "chrX": 20}


def _info(path, *args: str, outdir: Path | None = None) -> str:
    argv = ["wgsextract-cli", "info", *args, "--input", str(path)]
    argv += ["--outdir", str(outdir or Path(path).parent)]
    out = io.StringIO()
    with redirect_stdout(out), patch.object(sys, "argv", argv):
        main()
    return out.getvalue()


def _field(text: str, label: str) -> str:
    """The value printed after a `label` at the start of an info line."""
    return next(
        line[28:] for line in text.splitlines() if line.startswith(label)
    ).strip()


def _table(path, outdir: Path) -> dict[str, list[str]]:
    rows = csv.reader(io.StringIO(_info(path, "--detailed", "--csv", outdir=outdir)))
    next(rows)
    return {row[0]: row[1:] for row in rows}


@pytest.fixture
def no_remote_refs(tmp_path, monkeypatch):
    """Keep htslib from fetching a CRAM's reference by its M5 from the EBI server."""
    monkeypatch.setenv("REF_PATH", str(tmp_path / "no-ref-cache"))
    monkeypatch.setenv("REF_CACHE", str(tmp_path / "no-ref-cache"))


# --- fast and detailed metrics from real samtools ----------------------------


@needs_htslib
def test_fast_info_reports_reads_and_file_state(genome, tmp_path):
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    text = _info(bam)
    assert _field(text, "Avg Read Length") == "50 bp (SD=0 bp), Paired-end"
    assert _field(text, "Avg Insert Size") == "250 bp (SD=0 bp)"
    assert _field(text, "Sequencer") == "Illumina Novaseq 6000"
    assert _field(text, "File Stats") == "Sorted, Indexed, 0.0 GBs"
    assert _field(text, "File Format").startswith("BAM")
    assert _field(text, "MD5 Signature") == calculate_bam_md5(bam)
    # Fast mode doesn't count reads per contig, so it states no sex.
    assert "Bio Gender" not in text
    assert json.loads((tmp_path / "m.bam.wgse_info.json").read_text())[
        "avg_read_len"
    ] == pytest.approx(50)


@needs_htslib
def test_detailed_info_counts_reads_depth_and_sex(genome, tmp_path):
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    text = _info(bam, "--detailed")
    assert _field(text, "Bio Gender") == "Male"
    assert _field(text, "File Content") == "Auto, X, Y, Mito, Unmap"
    # 70 of the 72 reads are mapped.
    assert _field(text, "Reads").split()[:2] == ["97", "%"]

    table = _table(bam, tmp_path)
    # Model Len, N Len, # Segs Map, Map Gbases, Map ARD (segs x 50 bp / length)
    assert table["chr1"][:5] == ["1000", "0", "40", "0.00", "2"]
    assert table["chrX"][:5] == ["1000", "0", "20", "0.00", "1"]
    assert table["chrY"][2] == "8" and table["chrM"][2] == "2"
    assert table["Total"][:5] == ["3500", "0", "70", "0.00", "1"]


@needs_htslib
def test_female_total_leaves_out_chrY(genome, tmp_path):
    # One chrY pair against 20 chrX pairs is below the 5% male threshold.
    bam = _sample(genome, tmp_path / "f.bam", {**FEMALE, "chrY": 1})
    table = _table(bam, tmp_path)
    assert _field(_info(bam, "--detailed"), "Bio Gender") == "Female"
    assert table["chrY"][2] == "2"
    assert table["Total"][1:3] == ["0", "80"]  # chr1 + chrX only
    assert table["Total"][0] == "2000"


@needs_htslib
def test_numeric_contig_names_get_the_same_metrics(genome, tmp_path):
    rename = {"chr1": "1", "chrX": "X", "chrY": "Y", "chrM": "MT"}
    chr_bam = _sample(genome, tmp_path / "c.bam", MALE)
    num_bam = _sample(genome, tmp_path / "n.bam", MALE, rename=rename)
    chr_table, num_table = _table(chr_bam, tmp_path), _table(num_bam, tmp_path)
    assert {rename.get(k, k): v for k, v in chr_table.items()} == num_table
    text = _info(num_bam, "--detailed")
    assert _field(text, "Bio Gender") == "Male"
    assert _field(text, "File Content") == "Auto, X, Y, Mito, Unmap"


@needs_htslib
def test_unmapped_mates_are_not_counted_as_mapped(genome, tmp_path):
    seq = genome["seqs"]["chr1"]
    reads = []
    for i in range(10):
        pos = 1 + 50 * i
        name = f"m{i}"
        reads += [
            f"{name}\t73\tchr1\t{pos}\t60\t50M\t=\t{pos}\t0\t{seq[pos - 1 : pos + 49]}\t{QUAL}",
            f"{name}\t133\tchr1\t{pos}\t0\t*\t=\t{pos}\t0\t{'A' * 50}\t{QUAL}",
        ]  # fmt: skip
    header = [f"@SQ\tSN:{c}\tLN:{n}" for c, n in LENGTHS.items()]
    bam = sam_to_bam(header + reads, tmp_path / "half.bam")
    assert _field(_info(bam, "--detailed"), "Reads").split()[:2] == ["50", "%"]


# --- edge cases ---------------------------------------------------------------


@needs_htslib
def test_empty_bam_reports_what_it_cannot_compute(genome, tmp_path, capsys):
    header = [f"@SQ\tSN:{c}\tLN:{n}" for c, n in LENGTHS.items()]
    bam = sam_to_bam(header, tmp_path / "empty.bam")
    assert _field(_info(bam), "Avg Read Length") == "Could not compute"
    text = _info(bam, "--detailed")
    assert _field(text, "Reads").split()[:2] == ["0", "%"]
    assert _field(text, "File Content") == ""
    assert "low mapped average read depth" in capsys.readouterr().err


@needs_htslib
def test_unindexed_bam_still_gets_detailed_counts(genome, tmp_path):
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    Path(f"{bam}.bai").unlink()
    text = _info(bam, "--detailed")
    assert _field(text, "File Stats") == "Sorted, Unindexed, 0.0 GBs"
    assert _field(text, "Bio Gender") == "Male"


@needs_htslib
def test_name_sorted_bam_is_reported_unsorted(genome, tmp_path):
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    named = tmp_path / "n.bam"
    run_command(["samtools", "sort", "-n", "-o", str(named), bam])
    text = _info(named)
    assert _field(text, "File Stats") == "Unsorted, Unindexed, 0.0 GBs"
    assert _field(text, "Avg Insert Size") == "250 bp (SD=0 bp)"


def _cram_without_reference(genome, tmp_path: Path) -> Path:
    """A CRAM whose @SQ UR: points nowhere, so only --ref can decode it."""
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    cram = tmp_path / "m.cram"
    run_command(["samtools", "view", "-C", "-T", genome["ref"], "-o", str(cram), bam])
    header = run_command(["samtools", "view", "-H", str(cram)], capture_output=True)
    (tmp_path / "h.sam").write_text(header.stdout.replace(genome["ref"], "/gone.fa"))
    moved = tmp_path / "s.cram"
    with open(moved, "wb") as f:
        run_command(
            ["samtools", "reheader", str(tmp_path / "h.sam"), str(cram)], stdout=f
        )
    run_command(["samtools", "index", str(moved)])
    return moved


@needs_htslib
def test_cram_without_reference(genome, tmp_path, no_remote_refs, capsys):
    cram = _cram_without_reference(genome, tmp_path)
    # The header needs no reference; the reads do.
    text = _info(cram)
    assert _field(text, "Avg Read Length") == "Could not compute"
    assert _field(text, "File Stats") == "Sorted, Indexed, 0.0 GBs"
    with pytest.raises(SystemExit):
        _info(cram, "--detailed", outdir=tmp_path / "o")
    assert "--ref required for detailed mode with CRAM" in capsys.readouterr().err


@needs_htslib
def test_cram_with_reference(genome, tmp_path, no_remote_refs):
    cram = _cram_without_reference(genome, tmp_path)
    text = _info(cram, "--detailed", "--ref", genome["ref"])
    assert _field(text, "Avg Read Length") == "50 bp (SD=0 bp), Paired-end"
    assert _field(text, "Bio Gender") == "Male"
    assert _field(text, "File Format").startswith("CRAM")


# --- sample_sex ---------------------------------------------------------------


@needs_htslib
def test_sample_sex_from_idxstats_and_from_the_info_cache(genome, tmp_path):
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    assert metrics.sample_sex(bam) == "Male"
    _info(bam, "--detailed")
    with patch.object(metrics, "parse_idxstats", side_effect=AssertionError):
        assert metrics.sample_sex(bam) == "Male"


@needs_htslib
def test_sample_sex_unknown_when_idxstats_fails(tmp_path, caplog):
    bad = tmp_path / "bad.bam"
    bad.write_bytes(b"not a bam")
    assert metrics.sample_sex(str(bad)) == "Unknown"
    assert "Could not check chrY coverage" in caplog.text


@needs_htslib
def test_sample_sex_ignores_the_cache_of_a_replaced_file(genome, tmp_path):
    bam = tmp_path / "s.bam"
    _sample(genome, bam, MALE)
    _info(bam, "--detailed")
    # The file is replaced by a female sample aligned to the same reference.
    _sample(genome, tmp_path / "f.bam", FEMALE)
    shutil.move(tmp_path / "f.bam", bam)
    shutil.move(tmp_path / "f.bam.bai", f"{bam}.bai")
    assert metrics.sample_sex(str(bam)) == "Female"


# --- metrics helpers ----------------------------------------------------------


@needs_htslib
def test_body_sample_skips_secondary_supplementary_and_qc_fail(tmp_path):
    seq = "ACGT" * 25
    reads = [
        f"a\t99\tc\t1\t60\t100M\t=\t101\t300\t{seq}\t{'I' * 100}",
        f"a\t355\tc\t50\t0\t100M\t=\t101\t0\t{seq}\t{'I' * 100}",  # secondary
        f"a\t2147\tc\t60\t0\t10M\t=\t101\t0\t{seq[:10]}\t{'I' * 10}",  # supplementary
        f"b\t512\tc\t70\t60\t100M\t*\t0\t0\t{seq}\t{'I' * 100}",  # QC fail
        f"a\t147\tc\t101\t60\t60M\t=\t1\t-300\t{seq[:60]}\t{'I' * 60}",
        f"c\t0\tc\t200\t60\t80M\t*\t0\t0\t{seq[:80]}\t{'I' * 80}",
    ]
    bam = sam_to_bam(["@SQ\tSN:c\tLN:1000", *reads], tmp_path / "b.bam")
    count, avg_len, std_len, avg_tlen, std_tlen, paired, first = (
        metrics.run_body_sample(bam, [])
    )
    assert (count, avg_len, avg_tlen, std_tlen, paired, first) == (
        3, 80.0, 300.0, 0, True, "a",
    )  # fmt: skip
    assert std_len == pytest.approx((800 / 3) ** 0.5)


def test_body_sample_of_unreadable_file_returns_nothing(tmp_path):
    bad = tmp_path / "bad.bam"
    bad.write_bytes(b"junk")
    assert metrics.run_body_sample(str(bad), []) == (0, 0, 0, 0, 0, False, None)


@pytest.mark.parametrize(
    ("index", "indexed"),
    [
        ("s.bam.bai", True),
        ("s.bai", True),
        ("s.bam.csi", True),
        ("s.bam.crai", True),
        ("other.bai", False),
    ],
)
def test_file_stats_finds_each_index_name(tmp_path, index, indexed):
    (tmp_path / "s.bam").write_bytes(b"x" * 1024)
    (tmp_path / index).write_bytes(b"")
    size_gb, found = metrics.get_file_stats(str(tmp_path / "s.bam"))
    assert found is indexed and size_gb == 1024 / 1024**3


def test_file_stats_finds_replaced_extension_crai(tmp_path):
    (tmp_path / "s.cram").write_bytes(b"")
    (tmp_path / "s.crai").write_bytes(b"")
    assert metrics.get_file_stats(str(tmp_path / "s.cram"))[1] is True


@pytest.mark.parametrize(
    ("qname", "sequencer"),
    [
        (None, "Unknown"),
        ("read1", "Unknown"),
        ("A00910:1:ABCDEFGHI:1:1:1:1", "Illumina NS 6000 (Dante)"),
        ("E200006123L1C001R0010012345", "MGI DNB T7 (Dante, 6123 bad)"),
    ],
)
def test_determine_sequencer(qname, sequencer):
    assert metrics.determine_sequencer(qname) == sequencer


def test_load_n_counts_without_reference_or_sidecar(tmp_path):
    assert metrics.load_n_counts(None) == {}
    assert metrics.load_n_counts(str(tmp_path / "missing.fa")) == {}
    (tmp_path / "ref.fa").write_text(">c\nACGT\n")
    assert metrics.load_n_counts(str(tmp_path / "ref.fa")) == {}


def test_load_n_counts_skips_short_rows_and_rejects_binary(tmp_path, caplog):
    (tmp_path / "ref.fa").write_text(">c\nACGT\n")
    (tmp_path / "ref_ncnt.csv").write_text("chr1\nchr2\t100\t7\n")
    assert metrics.load_n_counts(str(tmp_path / "ref.fa")) == {"2": 7}
    (tmp_path / "ref_ncnt.csv").write_bytes(b"\xff\xfe\x00chr1\t1\t2\n")
    assert metrics.load_n_counts(str(tmp_path / "ref.fa")) == {}
    assert "Failed to read reference N counts" in caplog.text


def _stat(name, length, mapped, unmapped=0):
    return {"name": name, "length": length, "mapped": mapped, "unmapped": unmapped}


def test_infer_sex_thresholds():
    assert metrics.infer_sex([_stat("chrX", 1, 100), _stat("chrY", 1, 6)]) == "Male"
    assert metrics.infer_sex([_stat("X", 1, 100), _stat("Y", 1, 5)]) == "Female"
    assert metrics.infer_sex([_stat("chr1", 1, 100), _stat("chrY", 1, 5)]) == "Unknown"
    # Split contigs (e.g. chrY and chrY_random) of one key add up.
    assert (
        metrics.infer_sex(
            [_stat("chrX", 1, 100), _stat("chrY", 1, 3), _stat("CHRY", 1, 3)]
        )
        == "Male"
    )


def test_chrom_table_dog_cat_and_other_contigs():
    dog = metrics.generate_chrom_table(
        [_stat("chr38", 1000, 10), _stat("chrX", 1000, 10), _stat("chrY", 1000, 10)],
        100.0,
        "Female",
        "Dog_CanFam4",
    )
    # chr38 is a dog autosome; dogs have no chrY row, so it is "Other".
    assert [r[1] for r in dog] == ["chr38", "chrX", "Total", "Other"]
    assert dog[2][4] == 20 and dog[3][4] == 10

    cat = metrics.generate_chrom_table(
        [_stat("chrB1", 1000, 10), _stat("chrA1", 1000, 10), _stat("chrY", 1000, 5)],
        100.0,
        "Female",
        "Cat_Fca126",
    )
    # A cat's chrY counts toward the total even for a female call.
    assert [r[1] for r in cat] == ["chrA1", "chrB1", "chrY", "Total"]
    assert cat[3][4] == 25


def test_chrom_table_n_counts_from_the_reference_lower_effective_length():
    table = metrics.generate_chrom_table(
        [_stat("chr1", 1000, 10)], 100.0, "Unknown", "custom", n_counts={"1": 500}
    )
    assert table[0][3] == 500
    assert table[0][6] == 2  # 10 x 100 bp over 500 non-N bases


# --- runner -------------------------------------------------------------------


def test_info_requires_an_input(monkeypatch):
    monkeypatch.setattr(runner, "verify_dependencies", lambda _t: None)
    monkeypatch.setattr(runner, "log_dependency_info", lambda _t: None)
    args = type("A", (), {"input": None})()
    with pytest.raises(WGSExtractError, match="--input required"):
        runner.run(args)  # type: ignore[arg-type]


def test_cache_write_failure_is_only_a_warning(tmp_path, caplog):
    assert runner._save_cache(str(tmp_path / "missing" / "c.json"), {}) is False
    assert "Failed to write metrics cache" in caplog.text


@needs_htslib
def test_cache_from_before_health_checks_gets_them_added(genome, tmp_path):
    bam = _sample(genome, tmp_path / "m.bam", MALE)
    _info(bam)
    cache = tmp_path / "m.bam.wgse_info.json"
    data = json.loads(cache.read_text())
    del data["alignment_health"]
    cache.write_text(json.dumps(data))
    _info(bam)
    assert "alignment_health" in json.loads(cache.read_text())


@needs_htslib
def test_long_reads_are_warned_about(tmp_path, capsys):
    seq = "ACGT" * 125
    reads = [
        f"r{i}\t0\tchr1\t{1 + i}\t60\t500M\t*\t0\t0\t{seq}\t{'I' * 500}"
        for i in range(5)
    ]
    bam = sam_to_bam(["@SQ\tSN:chr1\tLN:1000", *reads], tmp_path / "long.bam")
    text = _info(bam, "--detailed")
    assert _field(text, "Avg Read Length") == "500 bp (SD=0 bp), Single-end"
    assert "long-read sequencer" in capsys.readouterr().err


# --- render -------------------------------------------------------------------


def test_render_detailed_extras():
    text = render.render_info(
        {
            "chrom_table_csv": "Seq Name,Model Len,Model N Len,# Segs Map,"
            "Map Gbases,Map ARD,Breadth Coverage\nchrM,500,0,0,0.00,0,\n",
            "refined_ns": True,
            "sequencer": "Unknown",
            "first_qname": "odd:read:name",
        },
        detailed=True,
    )
    row = next(line for line in text.splitlines() if line.startswith("chrM"))
    assert row.split() == ["chrM", "500", "0", "K", "0", "K", "0.00", "0", "x"]
    assert "Breadth" not in text.split("\n")[1]  # no coverage column
    assert "Active (using sidecar _ncnt.csv)" in text
    assert "Unknown: odd:read:name" in text
    # Fast output never shows a raw read name.
    assert "odd:read:name" not in render.render_info(
        {"sequencer": "Unknown", "first_qname": "odd:read:name"}
    )


def test_cram_coverage_passes_the_reference(tmp_path):
    ref = tmp_path / "ref.fa"
    ref.write_text(">c\nA\n")
    assert render._cram_reference_opts("x.CRAM", str(ref)) == ["--reference", str(ref)]
    assert render._cram_reference_opts("x.bam", str(ref)) == []
    assert render._cram_reference_opts("x.cram", str(tmp_path / "missing.fa")) == []


def test_unreadable_coverage_cache_is_recomputed(tmp_path, monkeypatch, caplog):
    sample = tmp_path / "s.bam"
    sample.write_bytes(b"")
    out = tmp_path / "s_coverage.tsv"
    out.write_text("chr1\tnot-a-number\t10\t1\t5\n")

    def fake_run(cmd, **_kw):
        Path(cmd[cmd.index("-o") + 1]).write_text(
            "#rname\tstartpos\tendpos\tnumreads\tcovbases\nchr1\t1\t100\t3\t25\n"
            "short\tline\n"
        )

    monkeypatch.setattr(render, "run_command", fake_run)
    assert render.run_full_coverage(str(sample), None, str(out)) == {
        "1": "25 %",
        "TOTAL_EST": "25 %",
    }
    assert "Ignoring unreadable coverage file" in caplog.text


@pytest.mark.parametrize("region", ["chr1:abc-10", "chr1:500-100", "chr1:0-10"])
def test_sampled_coverage_rejects_bad_regions(region):
    with pytest.raises(WGSExtractError, match="Invalid region"):
        render._resolve_sample_targets([_stat("chr1", 1000, 1)], region)


def test_sampled_coverage_needs_a_primary_chromosome(tmp_path):
    with pytest.raises(WGSExtractError, match="No primary chromosomes"):
        render.run_sampled_coverage(
            "x.bam", None, [_stat("chrUn_1", 500_000, 1)], str(tmp_path / "o.json")
        )
