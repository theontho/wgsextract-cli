import gzip
import logging
import random
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta
from wgsextract_cli.commands import realign
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError, run_command

FRAG = 300


def _args(source, **overrides):
    values = {
        "input": str(source),
        "ref": None,
        "outdir": None,
        "threads": "2",
        "memory": "100M",
        "r1": None,
        "r2": None,
        "input_ref": None,
        "force_extract": False,
        "no_preflight": True,
        "keep_fastq": False,
        "aligner": "auto",
        "platform": "illumina",
        "long_read": False,
        "preset": None,
        "gpu": False,
        "sample": None,
        "format": "BAM",
    }
    values.update(overrides)
    return Namespace(**values)


def _view(*args):
    return run_command(["samtools", *args], capture_output=True).stdout


def _old_alignment(tmp_path, genome, pairs=150):
    """A coordinate-sorted, indexed BAM of FR pairs on the genome under its old
    contig name ``old1``, with pair p0 duplicated as ``dup``."""
    rng = random.Random(11)
    starts = rng.sample(range(len(genome) - FRAG), pairs)
    names = [f"p{i}" for i in range(pairs)] + ["dup"]
    starts.append(starts[0])
    qual = "I" * 100
    lines = [
        "@HD\tVN:1.6\tSO:unsorted",
        f"@SQ\tSN:old1\tLN:{len(genome)}",
        "@RG\tID:old\tSM:OldName",
    ]
    for name, start in zip(names, starts, strict=True):
        mate = start + FRAG - 100
        lines += [
            f"{name}\t99\told1\t{start + 1}\t60\t100M\t=\t{mate + 1}\t{FRAG}\t"
            f"{genome[start : start + 100]}\t{qual}\tRG:Z:old",
            f"{name}\t147\told1\t{mate + 1}\t60\t100M\t=\t{start + 1}\t{-FRAG}\t"
            f"{genome[mate : mate + 100]}\t{qual}\tRG:Z:old",
        ]
    return sam_to_bam(lines, tmp_path / "old" / "x.bam"), pairs + 1


@pytest.fixture
def genome_and_ref(tmp_path):
    rng = random.Random(5)
    genome = "".join(rng.choice("ACGT") for _ in range(6_000))
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    ref = write_fasta(tmp_path / "new" / "ref.fa", {"chr1": genome})
    return genome, str(ref)


@needs_tools("bwa", "samtools")
def test_realign_bam_to_new_reference(tmp_path, genome_and_ref):
    genome, ref = genome_and_ref
    source, pairs = _old_alignment(tmp_path, genome)
    outdir = tmp_path / "out"

    realign.run(
        _args(source, ref=ref, outdir=str(outdir), sample="Ann", no_preflight=False)
    )

    out = outdir / "x_aligned.bam"
    assert (outdir / "x_aligned.bam.bai").is_file()
    header = _view("view", "-H", str(out))
    assert "SO:coordinate" in header
    assert f"@SQ\tSN:chr1\tLN:{len(genome)}" in header and "old1" not in header
    assert "\tSM:Ann\t" in header and "OldName" not in header
    reads = [r.split("\t") for r in _view("view", str(out)).splitlines()]
    assert len(reads) == 2 * pairs
    assert {r[2] for r in reads} == {"chr1"}
    positions = [int(r[3]) for r in reads]
    assert positions == sorted(positions)
    # p0 and dup are the same fragment: one of the two pairs is flagged.
    dups = [r[0] for r in reads if int(r[1]) & 0x400]
    assert len(dups) == 2 and len(set(dups)) == 1 and dups[0] in {"p0", "dup"}
    # The extracted FASTQs are removed once the alignment is done.
    assert sorted(p.name for p in outdir.iterdir()) == [
        "x_aligned.bam",
        "x_aligned.bam.bai",
    ]


@needs_tools("bwa", "samtools")
def test_realign_cram_decoded_with_library_reference_and_fastqs_kept(
    tmp_path, genome_and_ref
):
    genome, ref = genome_and_ref
    bam, pairs = _old_alignment(tmp_path, genome)
    # The CRAM's own reference is gone from where it was encoded: decoding
    # needs the library's copy, found by its sequence dictionary.
    encoded_with = write_fasta(tmp_path / "old" / "old.fa", {"old1": genome})
    cram = tmp_path / "old" / "x.cram"
    run_command(
        ["samtools", "view", "-C", "-T", str(encoded_with), "-o", str(cram), bam]
    )
    run_command(["samtools", "index", str(cram)])
    for path in tmp_path.joinpath("old").glob("old.fa*"):
        path.unlink()
    Path(bam).unlink()
    library = tmp_path / "lib"
    (library / "genomes").mkdir(parents=True)
    write_fasta(library / "genomes" / "old.fa", {"old1": genome})
    settings["reference_library"] = str(library)

    realign.run(_args(cram, ref=ref, keep_fastq=True))

    out = tmp_path / "old" / "x_aligned.bam"
    assert len(_view("view", str(out)).splitlines()) == 2 * pairs
    for mate in ("R1", "R2"):
        with gzip.open(tmp_path / "old" / f"x_{mate}.fastq.gz", "rt") as f:
            assert sum(1 for _ in f) == 4 * pairs


def test_realign_requires_existing_input(tmp_path):
    with pytest.raises(WGSExtractError, match="--input <BAM|CRAM> is required"):
        realign.run(_args(None, input=None))
    with pytest.raises(WGSExtractError, match="--input"):
        realign.run(_args(tmp_path / "missing.bam"))


def test_cram_input_ref_must_exist(tmp_path):
    with pytest.raises(WGSExtractError, match="--input-ref"):
        realign._input_cram_reference(
            Namespace(input="in.cram", input_ref=str(tmp_path / "gone.fa"))
        )
    (tmp_path / "old.fa").write_text(">c\nA\n")
    opt = realign._input_cram_reference(
        Namespace(input="in.CRAM", input_ref=str(tmp_path / "old.fa"))
    )
    assert opt == ["-T", str(tmp_path / "old.fa")]


def test_existing_fastqs_need_both_mates_and_prefer_the_input_dir(tmp_path):
    bam_dir, outdir = tmp_path / "bam", tmp_path / "out"
    bam_dir.mkdir()
    outdir.mkdir()
    source = bam_dir / "x.bam"
    source.write_bytes(b"")
    (bam_dir / "x_R1.fastq.gz").write_bytes(b"")
    assert realign._find_existing_fastqs(str(source), str(outdir)) is None

    for name in ("x_1.fq.gz", "x_2.fq.gz"):
        (outdir / name).write_bytes(b"")
    assert realign._find_existing_fastqs(str(source), str(outdir)) == (
        str(outdir / "x_1.fq.gz"),
        str(outdir / "x_2.fq.gz"),
    )

    for name in ("x.r1.fq.gz", "x.r2.fq.gz"):
        (bam_dir / name).write_bytes(b"")
    assert realign._find_existing_fastqs(str(source), str(outdir)) == (
        str(bam_dir / "x.r1.fq.gz"),
        str(bam_dir / "x.r2.fq.gz"),
    )


class _Fakes:
    """Stand-ins for extraction and alignment that record what realign asked."""

    def __init__(self, monkeypatch):
        self.extracted = []
        self.aligned = []
        self.align_error = None
        monkeypatch.setattr(realign, "verify_dependencies", lambda _tools: None)
        monkeypatch.setattr(realign, "log_dependency_info", lambda _tools: None)
        monkeypatch.setattr(realign, "alignments_to_fastq", self.extract)
        monkeypatch.setattr(realign.align_cmd, "run", self.align)

    def extract(self, _input, r1, r2, **_kwargs):
        self.extracted.append((r1, r2))
        for path in (r1, r2):
            Path(path).write_text("@r\nA\n+\nI\n")

    def align(self, args):
        self.aligned.append((args.r1, args.r2))
        if self.align_error:
            raise self.align_error


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "x.bam"
    path.write_bytes(b"data")
    return path


def test_existing_fastqs_are_reused_and_kept(tmp_path, monkeypatch, source):
    fakes = _Fakes(monkeypatch)
    pair = (str(tmp_path / "x_R1.fq.gz"), str(tmp_path / "x_R2.fq.gz"))
    for path in pair:
        Path(path).write_text("reads")

    realign.run(_args(source))

    assert fakes.extracted == [] and fakes.aligned == [pair]
    assert all(Path(p).read_text() == "reads" for p in pair)


def test_force_extract_reextracts_then_removes_the_fastqs(
    tmp_path, monkeypatch, source
):
    fakes = _Fakes(monkeypatch)
    for name in ("x_R1.fastq.gz", "x_R2.fastq.gz"):
        (tmp_path / name).write_text("stale")
    outdir = tmp_path / "out"

    realign.run(_args(source, outdir=str(outdir), force_extract=True))

    pair = (str(outdir / "x_R1.fastq.gz"), str(outdir / "x_R2.fastq.gz"))
    assert fakes.extracted == [pair] and fakes.aligned == [pair]
    assert not any(Path(p).exists() for p in pair)
    # The pair beside the input is not this run's to delete.
    assert (tmp_path / "x_R1.fastq.gz").read_text() == "stale"


def test_reads_passed_in_are_aligned_and_never_deleted(tmp_path, monkeypatch, source):
    fakes = _Fakes(monkeypatch)
    r1 = tmp_path / "reads.fq"
    r1.write_text("reads")

    realign.run(_args(source, r1=str(r1)))

    assert fakes.extracted == [] and fakes.aligned == [(str(r1), None)]
    assert r1.exists()
    with pytest.raises(WGSExtractError, match="--r2"):
        realign.run(_args(source, r1=str(r1), r2=str(tmp_path / "gone.fq")))


@pytest.mark.parametrize("error", [WGSExtractError("boom"), KeyboardInterrupt()])
def test_failed_extraction_leaves_no_truncated_fastqs(
    tmp_path, monkeypatch, source, error
):
    fakes = _Fakes(monkeypatch)

    def truncated(_input, r1, r2, **_kwargs):
        Path(r1).write_text("@r\nAC")
        raise error

    monkeypatch.setattr(realign, "alignments_to_fastq", truncated)
    with pytest.raises(type(error)):
        realign.run(_args(source))
    assert list(tmp_path.glob("*.fastq.gz")) == []
    assert fakes.aligned == []


def test_failed_alignment_keeps_fastqs_for_the_rerun(tmp_path, monkeypatch, source):
    fakes = _Fakes(monkeypatch)
    fakes.align_error = WGSExtractError("bwa alignment failed")
    with pytest.raises(WGSExtractError, match="bwa alignment failed"):
        realign.run(_args(source))
    pair = (str(tmp_path / "x_R1.fastq.gz"), str(tmp_path / "x_R2.fastq.gz"))
    assert all(Path(p).is_file() for p in pair)

    fakes.align_error = None
    realign.run(_args(source))
    assert fakes.extracted == [pair] and fakes.aligned == [pair, pair]
    # Reused, not extracted by this run: left in place.
    assert all(Path(p).is_file() for p in pair)


def test_cleanup_problem_does_not_fail_a_finished_realign(
    tmp_path, monkeypatch, source, caplog
):
    fakes = _Fakes(monkeypatch)
    monkeypatch.setattr(realign.align_cmd, "run", lambda args: Path(args.r1).unlink())
    with caplog.at_level(logging.WARNING):
        realign.run(_args(source))
    assert "Could not remove" in caplog.text
    assert fakes.extracted and not list(tmp_path.glob("*.fastq.gz"))


@pytest.mark.parametrize(("extract", "needed"), [(True, 10), (False, 5)])
def test_preflight_needs_room_for_the_extraction_only_when_extracting(
    tmp_path, monkeypatch, source, extract, needed
):
    fakes = _Fakes(monkeypatch)
    estimates = {"Name": (10, 4), "Coord": (3, 2)}  # (temp GB, final GB)
    monkeypatch.setattr(
        realign,
        "get_free_space_needed",
        lambda _size, sort_type, **_k: estimates[sort_type],
    )
    asked = []

    def no_room(path, gb):
        asked.append((path, gb))
        return False

    monkeypatch.setattr(realign, "check_free_space", no_room)
    reads = tmp_path / "reads.fq"
    reads.write_text("")
    args = _args(source, no_preflight=False, r1=None if extract else str(reads))

    # Extraction's temp space is freed before alignment; its FASTQs are not:
    # max(10, 4 + 3 + 2) when extracting, 3 + 2 when not.
    with pytest.raises(WGSExtractError, match=f"needs ~{needed} GB.*--no-preflight"):
        realign.run(args)
    assert asked == [(str(tmp_path), needed)]
    assert fakes.extracted == [] and fakes.aligned == []


@pytest.mark.parametrize("provided_fastqs", [False, True])
def test_realign_uses_actual_input_count_before_extraction_for_memory_and_disk(
    tmp_path, monkeypatch, source, provided_fastqs
):
    _Fakes(monkeypatch)
    ref = tmp_path / "ref.fa"
    ref.write_text(">c\nA\n")
    monkeypatch.setattr(realign.bwa_mem2, "check_index", lambda *_: None)

    def indexed_count(*_args):
        assert not provided_fastqs
        return 800_000_000

    monkeypatch.setattr(realign, "indexed_read_count", indexed_count)
    budgets = []
    monkeypatch.setattr(
        realign,
        "check_mapping_memory",
        lambda *_a, **kwargs: budgets.append(("memory", kwargs["reads"])),
    )
    monkeypatch.setattr(
        realign,
        "_preflight_disk_space",
        lambda *_a, **kwargs: budgets.append(("disk", kwargs["reads"])),
    )
    monkeypatch.setattr(
        realign.align_cmd,
        "run",
        lambda args: budgets.append(("align", args._input_read_count)),
    )
    overrides = {}
    if provided_fastqs:
        for mate in ("r1", "r2"):
            fastq = tmp_path / f"{mate}.fq"
            fastq.write_text("@r\nA\n+\nI\n")
            overrides[mate] = str(fastq)
    realign.run(
        _args(source, ref=str(ref), aligner="bwa-mem2", no_preflight=False, **overrides)
    )
    expected = 2 if provided_fastqs else 800_000_000
    assert budgets == [("memory", expected), ("disk", expected), ("align", expected)]


@pytest.mark.parametrize("no_preflight", [False, True])
@pytest.mark.parametrize("reads", [800_000_000, None])
def test_ultima_realign_counts_index_once_and_passes_through_unknown_counts(
    tmp_path, monkeypatch, source, no_preflight, reads
):
    calls = []

    def count(*_args):
        calls.append("idxstats")
        return reads

    monkeypatch.setattr(realign, "indexed_read_count", count)
    monkeypatch.setattr(
        realign,
        "_preflight_disk_space",
        lambda *_a, **k: calls.append(("disk", k["reads"])),
    )
    monkeypatch.setattr(
        realign.align_cmd,
        "align_ultima",
        lambda args, *_a: calls.append(("align", args._input_read_count)),
    )
    realign.run(
        _args(source, platform="ultima", aligner="bwa", no_preflight=no_preflight)
    )
    expected = ["idxstats"]
    if not no_preflight:
        expected.append(("disk", reads))
    expected.append(("align", reads))
    assert calls == expected
