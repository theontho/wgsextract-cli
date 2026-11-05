import argparse
import random
from argparse import Namespace
from io import StringIO

import pytest

from tests.htslib_helpers import needs_tools, write_fasta
from wgsextract_cli.commands import qc as _qc_entry
from wgsextract_cli.commands.qc import bam_writer as _qc_bam_writer
from wgsextract_cli.commands.qc import commands as _qc_commands
from wgsextract_cli.commands.qc import fake_data as _qc_fake_data
from wgsextract_cli.commands.qc.fake_data import generate_fake_genomics_data
from wgsextract_cli.core.utils import run_command


class _ModuleGroup:
    def __init__(self, *modules):
        object.__setattr__(self, "_modules", modules)

    def __getattr__(self, name):
        for module in self._modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        for module in self._modules:
            if hasattr(module, name):
                setattr(module, name, value)
        object.__setattr__(self, name, value)


qc = _ModuleGroup(_qc_entry, _qc_commands, _qc_bam_writer, _qc_fake_data)


def test_stream_fast_bam_sam_is_coordinate_sorted_and_human_like():
    output = StringIO()

    def noise(_chrom_idx: int, pos: int, length: int) -> str:
        source = "ACGT" * ((pos + length + 4) // 4)
        return source[pos % 4 : pos % 4 + length]

    qc._stream_fast_bam_sam(
        output.write,
        {"chr1": 1200, "chr2": 900},
        coverage=4.0,
        seed=42,
        target_md5="abc123",
        get_noise_seq=noise,
    )

    lines = output.getvalue().splitlines()
    assert "@RG\tID:sample1\tSM:sample1\tPL:ILLUMINA\tDS:MD5:abc123" in lines
    assert "@CO\tMD5:abc123" in lines

    records = [line.split("\t") for line in lines if not line.startswith("@")]
    assert records
    assert {record[1] for record in records} == {"99", "147"}
    assert any("NM:i:1" in record[11:] for record in records)

    last_chrom = None
    last_pos = 0
    chrom_order = {"chr1": 0, "chr2": 1}
    for record in records:
        chrom = record[2]
        pos = int(record[3])
        cigar = record[5]
        seq = record[9]
        qual = record[10]

        assert cigar.endswith("M")
        assert len(seq) == int(cigar[:-1])
        assert len(qual) == len(seq)
        if chrom != last_chrom:
            if last_chrom is not None:
                assert chrom_order[chrom] > chrom_order[last_chrom]
            last_chrom = chrom
            last_pos = 0
        assert pos >= last_pos
        last_pos = pos


def test_stream_fast_bam_sam_applies_consistent_snp_variants():
    output = StringIO()

    qc._stream_fast_bam_sam(
        output.write,
        {"chr1": 2500},
        coverage=50.0,
        seed=42,
        target_md5=None,
        get_noise_seq=lambda _chrom_idx, _pos, length: "A" * length,
    )

    records = [
        line.split("\t")
        for line in output.getvalue().splitlines()
        if not line.startswith("@")
    ]
    variant_pos = qc._first_fast_bam_variant_pos(0, 42)
    variant_base = qc._fast_bam_alt_base("A", 0, variant_pos, 42)
    variant_covering_records = [
        record
        for record in records
        if int(record[3]) <= variant_pos < int(record[3]) + len(record[9])
    ]

    assert variant_covering_records
    for record in variant_covering_records:
        read_offset = variant_pos - int(record[3])
        assert record[9][read_offset] == variant_base
        assert "NM:i:1" in record[11:]


def test_fast_bam_snps_are_exactly_the_reads_mismatches():
    rng = random.Random(3)
    genome = ["".join(rng.choice("ACGT") for _ in range(n)) for n in (9000, 5000)]

    def ref(chrom_idx, pos, length):
        return genome[chrom_idx][pos : pos + length]

    output = StringIO()
    qc._stream_fast_bam_sam(output.write, {"chr1": 9000, "chr2": 5000}, 10.0, 42,
                            None, ref)  # fmt: skip
    mismatches = set()
    for line in output.getvalue().splitlines():
        if line.startswith("@"):
            continue
        fields = line.split("\t")
        chrom_idx, start = int(fields[2][3:]) - 1, int(fields[3])
        for offset, base in enumerate(fields[9]):
            pos = start + offset
            if base != genome[chrom_idx][pos - 1]:
                mismatches.add((chrom_idx, pos, genome[chrom_idx][pos - 1], base))

    planted = {
        (chrom_idx, *snp)
        for chrom_idx, length in enumerate((9000, 5000))
        for snp in qc.fast_bam_snps(chrom_idx, length, 42, ref)
    }
    assert len(planted) == 8
    assert mismatches == planted


def _fasta(path):
    seqs: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            name = seqs.setdefault(line[1:].split()[0], [])
        else:
            name.append(line.strip())
    return {contig: "".join(parts) for contig, parts in seqs.items()}


def _bam(path):
    """(@SQ names and lengths, @HD line, reads as (contig, pos, flag, seq, NM))."""
    header = run_command(["samtools", "view", "-H", str(path)], capture_output=True)
    lines = header.stdout.splitlines()
    contigs = [
        tuple(field.split(":", 1)[1] for field in line.split("\t")[1:3])
        for line in lines
        if line.startswith("@SQ")
    ]
    body = run_command(["samtools", "view", str(path)], capture_output=True)
    reads = []
    for line in body.stdout.splitlines():
        fields = line.split("\t")
        nm = next(int(tag[5:]) for tag in fields[11:] if tag.startswith("NM:i:"))
        reads.append((fields[2], int(fields[3]), int(fields[1]), fields[9], nm))
    return [(name, int(length)) for name, length in contigs], lines[0], reads


def _mismatches(read, genome):
    contig, pos, _flag, seq, _nm = read
    ref = genome[contig][pos - 1 : pos - 1 + len(seq)]
    return sum(a != b for a, b in zip(seq, ref, strict=True))


@needs_tools("samtools")
@pytest.mark.parametrize(
    "build, contigs",
    [
        ("hg38", [*(f"chr{n}" for n in (*range(1, 23), "X", "Y", "M")), "chrExtra"]),
        ("GRCh37", [*(str(n) for n in (*range(1, 23), "X", "Y", "MT")), "Extra"]),
    ],
)
def test_fake_bam_is_sorted_paired_reads_of_the_reference_it_writes(
    tmp_path, build, contigs
):
    generate_fake_genomics_data(
        str(tmp_path), coverage=0.02, seed=1, build=build, types=["bam"]
    )

    sq, hd, reads = _bam(tmp_path / "fake.bam")
    genome = _fasta(next(tmp_path.glob("fake_ref_*.fa")))
    assert [name for name, _ in sq] == contigs == list(genome)
    assert all(len(genome[name]) == length for name, length in sq)
    assert "SO:coordinate" in hd
    assert (tmp_path / "fake.bam.bai").exists()
    assert {read[2] for read in reads} == {99, 147}
    assert {read[0] for read in reads} == set(contigs)
    order = [(contigs.index(read[0]), read[1]) for read in reads]
    assert order == sorted(order)
    # Reads match the reference apart from the planted SNPs, which NM counts.
    assert all(_mismatches(read, genome) == read[4] <= 1 for read in reads)
    assert any(read[4] for read in reads)


@needs_tools("samtools")
def test_fake_bam_reads_follow_a_supplied_reference(tmp_path):
    rng = random.Random(5)
    mine = {"chr1": "".join(rng.choice("ACGT") for _ in range(501_000))}
    ref = write_fasta(tmp_path / "mine.fa", mine)
    before = ref.read_bytes()
    outdir = tmp_path / "out"
    outdir.mkdir()

    generate_fake_genomics_data(
        str(outdir), ref_path=str(ref), coverage=0.05, seed=1, types=["bam"]
    )

    _, _, reads = _bam(outdir / "fake.bam")
    on_mine = [read for read in reads if read[0] == "chr1"]
    assert on_mine
    assert all(_mismatches(read, mine) == read[4] <= 1 for read in on_mine)
    # Contigs the reference lacks still get (synthetic) reads.
    assert {read[0] for read in reads} > {"chr1", "chr2"}
    assert ref.read_bytes() == before
    assert not list(outdir.glob("*.fa"))


@needs_tools("samtools")
@pytest.mark.parametrize(
    "build, first, length", [("hg38", "chr1", 248956422), ("GRCh37", "1", 249250621)]
)
def test_full_size_fake_bam_has_human_contig_lengths_without_a_reference(
    tmp_path, build, first, length
):
    generate_fake_genomics_data(
        str(tmp_path),
        coverage=0.00001,
        seed=1,
        build=build,
        full_size=True,
        types=["bam"],
    )

    sq, _, reads = _bam(tmp_path / "fake.bam")
    assert sq[0] == (first, length)
    assert len(sq) == 26
    assert {read[0] for read in reads} >= {name for name, _ in sq[:24]}
    # A BAM alone doesn't need the 3 GB FASTA.
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "fake.bam",
        "fake.bam.bai",
    ]


def test_qc_fake_data_accepts_build_aliases() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    qc.register(subparsers, argparse.ArgumentParser(add_help=False))

    args = parser.parse_args(["qc", "fake-data", "--build", "GRCh37"])
    assert args.build == "GRCh37"

    args = parser.parse_args(["qc", "fake-data", "--build", "hs38d1"])
    assert args.build == "hs38d1"


def test_cmd_fake_data_rejects_legacy_bam_with_full_size(monkeypatch, tmp_path):
    monkeypatch.setattr(qc, "verify_dependencies", lambda _tools: None)
    monkeypatch.setattr(qc, "log_dependency_info", lambda _tools: None)

    args = Namespace(
        outdir=str(tmp_path),
        coverage=1.0,
        seed=1,
        build="hg38",
        full_size=True,
        type="bam",
        ref=None,
        legacy_bam=True,
    )

    try:
        qc.cmd_fake_data(args)
    except qc.WGSExtractError as exc:
        assert "--legacy-bam" in str(exc)
    else:
        raise AssertionError("Expected --legacy-bam with --full-size to fail")


def test_create_fast_fake_bam_kills_samtools_on_unexpected_stream_error(
    monkeypatch, tmp_path
):
    class FailingStdin:
        def write(self, _data: bytes):
            raise ValueError("stream interrupted")

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdin = FailingStdin()
            self._running = True
            self.killed = False
            self.waited = False

        def wait(self) -> int:
            self.waited = True
            self._running = False
            return 0

        def poll(self) -> int | None:
            return None if self._running else 0

        def kill(self):
            self.killed = True
            self._running = False

    process = FakeProcess()

    monkeypatch.setattr(qc, "popen", lambda _cmd, stdin: process)
    monkeypatch.setattr(qc, "get_tool_path", lambda _tool: "samtools")

    def noise(_chrom_idx: int, pos: int, length: int) -> str:
        source = "ACGT" * ((pos + length + 4) // 4)
        return source[pos % 4 : pos % 4 + length]

    with pytest.raises(ValueError, match="stream interrupted"):
        qc._create_fast_fake_bam(
            str(tmp_path / "fake.bam"),
            {"chr1": 800},
            coverage=1.0,
            seed=7,
            target_md5=None,
            get_noise_seq=noise,
            threads="2",
        )

    assert process.killed
    assert process.waited
