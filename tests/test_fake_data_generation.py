"""`qc fake-data` output checked with samtools/bcftools: the e2e suites and the
benchmark build on these files, so they must be valid, consistent and
reproducible."""

import gzip
import re
import shutil
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_htslib, run_wgsextract
from wgsextract_cli.commands import qc as qc_entry
from wgsextract_cli.commands.qc import bam_writer, fake_data
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

SCALED_HG38 = {f"chr{i}": 500000 + i * 1000 for i in range(1, 23)} | {
    "chrX": 600000,
    "chrY": 400000,
    "chrM": 16569,
    "chrExtra": 1000,
}


def _samtools(*argv: str) -> str:
    return run_command([get_tool_path("samtools"), *argv], capture_output=True).stdout


def _bcftools(*argv: str) -> str:
    return run_command([get_tool_path("bcftools"), *argv], capture_output=True).stdout


def _rows(text: str) -> list[list[str]]:
    return [line.split("\t") for line in text.splitlines()]


def _contig_counts(vcf: Path) -> dict[str, int]:
    return {f[0]: int(f[2]) for f in _rows(_bcftools("index", "--stats", str(vcf)))}


def _records(alignment: Path, *opts: str, region: str = "") -> list[list[str]]:
    return _rows(
        _samtools("view", *opts, str(alignment), *([region] if region else []))
    )


def _read_fasta(path: Path) -> dict[str, str]:
    seqs: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            seqs[line[1:]] = []
        else:
            seqs[next(reversed(seqs))].append(line)
    return {name: "".join(parts) for name, parts in seqs.items()}


def _fastq_records(path: Path) -> list[tuple[str, str]]:
    with gzip.open(path, "rt") as handle:
        lines = handle.read().splitlines()
    return [(lines[i][1:], lines[i + 1]) for i in range(0, len(lines), 4)]


def _mismatches(ref: str, record: list[str]) -> list[int]:
    """1-based reference positions where an M/D-only read differs from ``ref``."""
    rpos, qpos, seq, out = int(record[3]) - 1, 0, record[9], []
    for length, op in re.findall(r"(\d+)([MD])", record[5]):
        n = int(length)
        if op == "M":
            out += [rpos + k + 1 for k in range(n) if ref[rpos + k] != seq[qpos + k]]
            qpos += n
        rpos += n
    return out


def _generate(outdir: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> Path:
    monkeypatch.setattr(fake_data, "get_resource_defaults", lambda *_: ("2", "1G"))
    outdir.mkdir(parents=True, exist_ok=True)
    fake_data.generate_fake_genomics_data(str(outdir), **kwargs)
    return outdir


@pytest.fixture(scope="module")
def scaled(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Every output type of the default scaled hg38 generator at 0.5x."""
    if not all(get_tool_path(t) for t in ("samtools", "bcftools", "bgzip", "tabix")):
        pytest.skip("htslib tools missing")
    with pytest.MonkeyPatch.context() as mp:
        yield _generate(
            tmp_path_factory.mktemp("fake"),
            mp,
            coverage=0.5,
            seed=7,
            types=["bam", "cram", "vcf", "fastq"],
            target_md5="0123abcd",
        )


@needs_htslib
def test_scaled_bam_is_sorted_indexed_and_has_requested_contigs_and_depth(scaled):
    bam = scaled / "fake.bam"
    header = _samtools("view", "-H", str(bam)).splitlines()
    assert header[0] == "@HD\tVN:1.6\tSO:coordinate"
    assert "@CO\tMD5:0123abcd" in header
    contigs = {
        m["sn"]: int(m["ln"])
        for m in (re.match(r"@SQ\tSN:(?P<sn>\S+)\tLN:(?P<ln>\d+)", h) for h in header)
        if m
    }
    assert contigs == SCALED_HG38

    idx = {f[0]: int(f[2]) for f in _rows(_samtools("idxstats", str(bam)))}
    assert all(idx[name] > 0 for name in SCALED_HG38)
    depth = {f[0]: float(f[6]) for f in _rows(_samtools("coverage", str(bam)))[1:]}
    for name in ("chr1", "chrX", "chrM"):
        assert depth[name] == pytest.approx(0.5, rel=0.05), name

    order = list(SCALED_HG38)
    keys = [(order.index(r[2]), int(r[3])) for r in _records(bam)]
    assert keys == sorted(keys)


@needs_htslib
def test_read_pairs_are_proper_and_mates_agree(scaled):
    records = _records(scaled / "fake.bam", region="chrM")
    pairs: dict[str, dict[str, list[str]]] = {}
    for r in records:
        pairs.setdefault(r[0], {})[r[1]] = r
    assert pairs and all(set(p) == {"99", "147"} for p in pairs.values())
    for p in pairs.values():
        r1, r2 = p["99"], p["147"]
        assert (r1[7], r2[7]) == (r2[3], r1[3])
        r2_end = int(r2[3]) + len(r2[9]) - 1
        assert int(r1[8]) == r2_end - int(r1[3]) + 1 == -int(r2[8])
        assert re.fullmatch(r"\d+M", r1[5]) and int(r1[5][:-1]) == len(r1[9])


@needs_htslib
def test_reads_match_reference_except_planted_homozygous_snps(scaled):
    ref = _read_fasta(scaled / "fake_ref_hg38_scaled.fa")["chr2"]
    chrom_idx = list(SCALED_HG38).index("chr2")
    first = bam_writer._first_fast_bam_variant_pos(chrom_idx, 7)
    planted = {
        pos: bam_writer._fast_bam_alt_base(ref[pos - 1], chrom_idx, pos, 7)
        for pos in range(first, len(ref) + 1, 2000)
    }
    seen: set[int] = set()
    for r in _records(scaled / "fake.bam", region="chr2"):
        diffs = _mismatches(ref, r)
        start, end = int(r[3]), int(r[3]) + len(r[9]) - 1
        covered = [p for p in planted if start <= p <= end]
        assert diffs == covered, r[0]
        assert all(r[9][p - start] == planted[p] for p in covered)
        assert f"NM:i:{len(diffs)}" in r[11:]
        seen.update(covered)
    assert len(seen) >= 10


@needs_htslib
def test_cram_decodes_to_the_bam_records_with_generated_reference(scaled):
    ref = str(scaled / "fake_ref_hg38_scaled.fa")
    assert not (scaled / "fake.sam").exists()
    cram = [r[:11] for r in _records(scaled / "fake.cram", "-T", ref)]
    assert cram == [r[:11] for r in _records(scaled / "fake.bam")]
    _samtools("quickcheck", str(scaled / "fake.cram"))
    assert (scaled / "fake.cram.crai").stat().st_size > 0


@needs_htslib
def test_fastq_pairs_are_in_step_and_hold_every_read(scaled):
    r1 = _fastq_records(scaled / "fake_R1.fastq.gz")
    r2 = _fastq_records(scaled / "fake_R2.fastq.gz")
    assert [n for n, _ in r1] == [n for n, _ in r2]
    first_mates = {r[0]: r[9] for r in _records(scaled / "fake.bam", "-f", "64")}
    assert dict(r1) == first_mates


@needs_htslib
def test_vcf_is_indexed_with_variants_on_every_contig(scaled):
    vcf = scaled / "fake.vcf.gz"
    assert not (scaled / "fake.vcf").exists()
    header = _bcftools("view", "-h", str(vcf))
    for name, length in SCALED_HG38.items():
        assert f"##contig=<ID={name},length={length}>" in header
    counts = _contig_counts(vcf)
    assert set(counts) == set(SCALED_HG38)
    # The SNPs planted in the reads: homozygous, one every 2 kb.
    assert counts["chr1"] in (250, 251)
    calls = _bcftools("query", "-f", "%REF %ALT [%GT]\n", str(vcf)).splitlines()
    assert {c.split()[2] for c in calls} == {"1/1"}
    assert all(c.split()[0] != c.split()[1] for c in calls)


@needs_htslib
def test_vcf_ref_alleles_match_generated_reference(scaled):
    _bcftools(
        "norm",
        "--check-ref",
        "e",
        "-f",
        str(scaled / "fake_ref_hg38_scaled.fa"),
        "-o",
        str(scaled / "normed.vcf"),
        str(scaled / "fake.vcf.gz"),
    )


@needs_htslib
def test_same_seed_reproduces_outputs_and_another_seed_differs(tmp_path, monkeypatch):
    def outputs(name: str, seed: int) -> tuple[list[list[str]], str, str]:
        out = _generate(
            tmp_path / name, monkeypatch, coverage=0.05, seed=seed, types=["bam", "vcf"]
        )
        return (
            _records(out / "fake.bam"),
            _bcftools("view", "-H", str(out / "fake.vcf.gz")),
            (out / "fake_ref_hg38_scaled.fa").read_text(),
        )

    first = outputs("a", 3)
    assert outputs("b", 3) == first
    other = outputs("c", 4)
    assert all(x != y for x, y in zip(other, first, strict=True))


@needs_htslib
def test_cram_only_removes_intermediate_bam_and_uses_hg37_names(tmp_path, monkeypatch):
    out = _generate(tmp_path, monkeypatch, coverage=0.02, build="hs37d5")
    assert sorted(p.name for p in out.iterdir()) == [
        "fake.cram",
        "fake.cram.crai",
        "fake_ref_hs37d5_scaled.fa",
        "fake_ref_hs37d5_scaled.fa.fai",
    ]
    header = _rows(_samtools("view", "-H", str(out / "fake.cram")))
    names = [row[1] for row in header if row[0] == "@SQ"]
    assert names[0] == "SN:1" and "SN:MT" in names and "SN:Extra" in names


@needs_htslib
def test_cli_writes_reference_to_requested_missing_ref_path(tmp_path):
    """The benchmark runs `qc fake-data --ref <dataset>/benchmark_ref.fa` and
    expects that file to be created there."""
    ref = tmp_path / "benchmark_ref.fa"
    run_wgsextract(
        "qc",
        "fake-data",
        "--outdir",
        str(tmp_path),
        "--ref",
        str(ref),
        "--type",
        "bam",
        "--coverage",
        "0.02",
        "--threads",
        "2",
    )
    assert ref.exists() and Path(f"{ref}.fai").exists()
    assert (tmp_path / "fake.bam.bai").exists()
    assert not list(tmp_path.glob("fake_ref_*"))
    assert _read_fasta(ref)["chr1"][:50] == _samtools(
        "faidx", str(ref), "chr1:1-50"
    ).split("\n", 1)[1].replace("\n", "")


@needs_htslib
def test_existing_reference_backs_the_reads(tmp_path, monkeypatch):
    """A caller-supplied FASTA is reused untouched and the reads are drawn from it."""
    chr1 = ("ACGT" * 125250)[:501000]
    ref = tmp_path / "given.fa"
    ref.write_text(f">chr1\n{chr1}\n")
    _generate(tmp_path, monkeypatch, ref_path=str(ref), coverage=0.05, types=["bam"])
    assert ref.read_text() == f">chr1\n{chr1}\n"
    for r in _records(tmp_path / "fake.bam", region="chr1"):
        assert len(_mismatches(chr1, r)) == int(
            re.search(r"NM:i:(\d+)", "\t".join(r[11:]))[1]
        )
        assert (
            _mismatches(chr1, r)
            or r[9] == chr1[int(r[3]) - 1 : int(r[3]) - 1 + len(r[9])]
        )


@needs_htslib
def test_fastq_pairs_stay_in_step_at_high_coverage(tmp_path, monkeypatch):
    real = fake_data._create_fast_fake_bam

    def one_small_contig(bam_path, chroms, *args):
        real(bam_path, {"chr1": 20000}, *args)

    monkeypatch.setattr(fake_data, "_create_fast_fake_bam", one_small_contig)
    _generate(tmp_path, monkeypatch, coverage=20.0, types=["fastq"])
    assert not (tmp_path / "fake.bam").exists()
    r1 = _fastq_records(tmp_path / "fake_R1.fastq.gz")
    r2 = _fastq_records(tmp_path / "fake_R2.fastq.gz")
    assert len(r1) == len(r2) == 2000
    assert [n for n, _ in r1] == [n for n, _ in r2]


@needs_htslib
def test_legacy_bam_is_valid_and_marks_deletions(tmp_path, monkeypatch):
    _generate(
        tmp_path, monkeypatch, coverage=0.2, seed=5, types=["bam"], legacy_bam=True
    )
    bam = tmp_path / "fake.bam"
    _samtools("quickcheck", str(bam))
    assert not (tmp_path / "fake.sam").exists()
    records = _records(bam)
    assert any("D" in r[5] for r in records)
    order = list(SCALED_HG38)
    keys = [(order.index(r[2]), int(r[3])) for r in records]
    assert keys == sorted(keys)
    assert {r[1] for r in records} == {"99", "147"}


@needs_htslib
def test_legacy_reads_differ_from_reference_only_at_planted_variants(
    tmp_path, monkeypatch
):
    _generate(
        tmp_path, monkeypatch, coverage=0.5, seed=2, types=["bam"], legacy_bam=True
    )
    ref = _read_fasta(tmp_path / "fake_ref_hg38_scaled.fa")
    worst = max(
        _records(tmp_path / "fake.bam", region="chr16"),
        key=lambda r: len(_mismatches(ref["chr16"], r)),
    )
    assert len(_mismatches(ref["chr16"], worst)) <= 3, worst[:9]


@pytest.mark.parametrize(
    ("build", "chr1", "mito", "extra"),
    [
        ("hg38", ("chr1", 248956422), "chrM", "chrExtra"),
        ("hs37d5", ("1", 249250621), "MT", "Extra"),
        ("t2t", ("chr1", 248387328), "chrM", "chrExtra"),
    ],
)
def test_full_size_bam_uses_real_lengths_and_skips_reference(
    tmp_path, monkeypatch, build, chr1, mito, extra
):
    seen = {}

    def capture(bam_path, chroms, coverage, seed, target_md5, provider, threads):
        seen.update(chroms=chroms, sample=provider(0, 0, 8), threads=threads)

    monkeypatch.setattr(fake_data, "_create_fast_fake_bam", capture)
    monkeypatch.setattr(fake_data, "run_command", lambda *a, **k: None)
    _generate(
        tmp_path, monkeypatch, full_size=True, coverage=0.01, build=build, types=["bam"]
    )
    chroms = seen["chroms"]
    assert len(chroms) == 26
    assert (
        chroms[chr1[0]] == chr1[1] and chroms[mito] == 16569 and chroms[extra] == 1000
    )
    assert re.fullmatch("[ACGT]{8}", seen["sample"])
    assert seen["threads"] == "2"
    assert list(tmp_path.iterdir()) == []


def test_full_size_refuses_when_disk_is_too_small(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: (0, 0, 5 * 1024**3))
    monkeypatch.setattr(
        fake_data, "_create_fast_fake_bam", lambda *a: pytest.fail("generated anyway")
    )
    with pytest.raises(WGSExtractError, match=r"Need at least 9\.0GB"):
        _generate(tmp_path, monkeypatch, full_size=True, coverage=2.0, types=["bam"])


@needs_htslib
def test_full_size_vcf_density_follows_coverage(tmp_path, monkeypatch):
    _generate(tmp_path, monkeypatch, full_size=True, coverage=0.5, types=["vcf"])
    counts = _contig_counts(tmp_path / "fake.vcf.gz")
    assert counts["chr1"] == 248956422 // 21000
    assert counts["chrM"] == 2
    assert not list(tmp_path.glob("*.fa"))


@pytest.mark.parametrize(
    ("type_arg", "expected"),
    [("all", ["vcf", "cram", "bam", "fastq"]), (" BAM , vcf", ["bam", "vcf"])],
)
def test_cmd_fake_data_parses_types_and_uses_installed_library_genome(
    tmp_path, monkeypatch, type_arg, expected
):
    from wgsextract_cli.core import ref_library
    from wgsextract_cli.core.config import settings

    genome = tmp_path / "lib" / "genomes" / "hg38.fa.gz"
    genome.parent.mkdir(parents=True)
    genome.write_bytes(b"")
    monkeypatch.setitem(settings, "reference_library", str(tmp_path / "lib"))
    monkeypatch.setattr(
        ref_library,
        "get_available_genomes",
        lambda: [
            {"code": "hs37d5", "final": "hs37d5.fa.gz", "md5": "aaa"},
            {"code": "hg38", "final": "hg38.fa.gz", "md5": "bbb"},
        ],
    )
    monkeypatch.setattr(qc_entry, "verify_dependencies", lambda _t: None)
    monkeypatch.setattr(qc_entry, "log_dependency_info", lambda _t: None)
    calls = []
    monkeypatch.setattr(
        qc_entry, "generate_fake_genomics_data", lambda *a, **k: calls.append((a, k))
    )
    outdir = tmp_path / "new" / "dir"
    qc_entry.cmd_fake_data(
        Namespace(
            outdir=str(outdir),
            ref=None,
            build="hg38",
            type=type_arg,
            coverage=2.0,
            seed=9,
            full_size=False,
            legacy_bam=False,
        )
    )
    assert outdir.is_dir()
    ((args, kwargs),) = calls
    assert args == (str(outdir), str(genome))
    assert kwargs["types"] == expected
    assert kwargs["target_md5"] == "bbb"
    assert (kwargs["coverage"], kwargs["seed"]) == (2.0, 9)


def test_cmd_fake_data_ignores_ref_directory_without_fasta(tmp_path, monkeypatch):
    monkeypatch.setattr(qc_entry, "verify_dependencies", lambda _t: None)
    monkeypatch.setattr(qc_entry, "log_dependency_info", lambda _t: None)
    calls = []
    monkeypatch.setattr(
        qc_entry, "generate_fake_genomics_data", lambda *a, **k: calls.append(a)
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    qc_entry.cmd_fake_data(
        Namespace(
            outdir=str(tmp_path),
            ref=str(empty),
            build="hg38",
            type="vcf",
            coverage=1.0,
            seed=1,
            full_size=False,
            legacy_bam=False,
        )
    )
    assert calls == [(str(tmp_path), None)]


def test_legacy_variants_keep_every_deletion_and_shift_later_snps():
    ref = "".join("ACGT"[i % 4] for i in range(100))
    variants = {
        150: ("A", "AT", True),  # deletion at read offset 50
        130: ("A", "AT", True),  # deletion at read offset 30
        162: ("A", "T", False),  # SNP after both deletions
        152: ("A", "T", False),  # inside the second deletion: dropped
    }
    seq, cigar = fake_data._apply_legacy_variants(list(ref), 100, variants)
    assert cigar == "30M5D15M5D45M"
    assert seq == ref[:30] + ref[35:50] + ref[55:62] + "T" + ref[63:]
