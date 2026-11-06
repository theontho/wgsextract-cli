import gzip
import json
import logging
import os
import platform
import random
import re
import shutil
import sys
from argparse import Namespace
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.fake_tools import FakeParabricks
from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta
from wgsextract_cli.commands import realign
from wgsextract_cli.core import alignment_memory, parabricks
from wgsextract_cli.core.alignment_memory import MappingMemory
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.read_counts import AlignmentSample, UnpairedSample
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
    assert sorted(p.name for p in outdir.iterdir() if p.name[0] != ".") == [
        "x_aligned.bam",
        "x_aligned.bam.bai",
        "x_aligned.bam.steps.json",
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


def _write_alignment(args):
    """What a finished alignment leaves: the BAM and its index."""
    outdir = args.outdir or str(Path(args.input).parent)
    out = realign.align_cmd.aligned_output_path(outdir, args.r1, args.r2, args.format)
    Path(out).write_text("aligned")
    Path(out + ".bai").write_text("index")


def _visible(directory):
    """A directory's files, less the step journal and hidden lock files."""
    return [
        p
        for p in Path(directory).iterdir()
        if not p.name.startswith(".") and not p.name.endswith(".steps.json")
    ]


def _published(staged):
    path = Path(staged)
    match = re.fullmatch(r"\.(.+)\.[0-9a-f]{32}\.partial(.*)", path.name)
    return str(path.with_name(match[1] + match[2])) if match else staged


class _Fakes:
    """Stand-ins for extraction and alignment that record what realign asked."""

    def __init__(self, monkeypatch):
        self.extracted = []
        self.aligned = []
        self.streamed = []
        self.align_error = None
        monkeypatch.setattr(realign, "verify_dependencies", lambda _tools: None)
        monkeypatch.setattr(realign, "log_dependency_info", lambda _tools: None)
        monkeypatch.setattr(realign, "alignments_to_fastq", self.extract)
        monkeypatch.setattr(realign.align_cmd, "run", self.align)
        monkeypatch.setattr(realign.align_cmd, "align_bwa", self.stream)
        monkeypatch.setattr(
            realign, "alignment_sample", lambda *_a, **_k: AlignmentSample(100, 400)
        )
        monkeypatch.setattr(
            realign, "unpaired_sample", lambda *_a, **_k: UnpairedSample(2, 0, None)
        )
        monkeypatch.setattr(realign.align_cmd, "available_memory", lambda: 1024**4)
        monkeypatch.setattr(realign, "available_memory", lambda: 1024**4)

    def extract(self, _input, r1, r2, **_kwargs):
        # Extraction writes staged copies; record the FASTQs they become.
        self.extracted.append(tuple(map(_published, (r1, r2))))
        for path in (r1, r2):
            Path(path).write_text("@r\nA\n+\nI\n")

    def align(self, args):
        self.aligned.append((args.r1, args.r2))
        if self.align_error:
            raise self.align_error
        _write_alignment(args)

    def stream(self, args, **kwargs):
        self.streamed.append(kwargs)
        self.align(args)


_DISK = re.compile(r"Realign pre-flight: ~(\d+) GB .*\(extract (\d+)\+(\d+), align")


def _disk_preflights(caplog) -> list[tuple[int, int]]:
    """(GB needed, of which GB of FASTQs kept until alignment) per pre-flight."""
    found = [_DISK.search(r.getMessage()) for r in caplog.records]
    return [(int(m[1]), int(m[3])) for m in found if m]


def _preflight_gib(caplog, kind: str) -> float:
    """The first estimate of a "Preflight memory"/"Preflight disk" report line."""
    line = next(
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(f"Preflight {kind}")
    )
    return float(line.split()[3])


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


@pytest.fixture
def roomy_disk(monkeypatch):
    """A disk with room for any estimate, so the pre-flight check passes."""
    roomy = _DiskUsage(2**60, 0, 2**60)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: roomy)


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


@needs_tools("samtools")
def test_mem2_realign_prepares_missing_fai_before_index_preflight(
    tmp_path, monkeypatch, source
):
    fakes = _Fakes(monkeypatch)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 8 * 1024**3)
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nACGT\n")
    pair = (str(tmp_path / "r1.fq"), str(tmp_path / "r2.fq"))
    for path in pair:
        Path(path).write_text("@r\nA\n+\nI\n")
    assert not Path(str(ref) + ".fai").exists()
    assert not Path(str(ref) + ".ann").exists()

    realign.run(_args(source, ref=str(ref), aligner="bwa-mem2", r1=pair[0], r2=pair[1]))

    assert Path(str(ref) + ".fai").read_text().split("\t")[:2] == ["chr1", "4"]
    assert fakes.aligned == [pair]


def test_force_extract_streams_without_touching_existing_fastqs(
    tmp_path, monkeypatch, source
):
    fakes = _Fakes(monkeypatch)
    for name in ("x_R1.fastq.gz", "x_R2.fastq.gz"):
        (tmp_path / name).write_text("stale")
    outdir = tmp_path / "out"

    realign.run(_args(source, outdir=str(outdir), force_extract=True))

    pair = (str(outdir / "x_R1.fastq.gz"), str(outdir / "x_R2.fastq.gz"))
    assert fakes.extracted == [] and fakes.aligned == [pair]
    assert fakes.streamed[0]["read_paths"] == [str(source)]
    assert not any(Path(p).exists() for p in pair)
    # The pair beside the input is not this run's to delete.
    assert (tmp_path / "x_R1.fastq.gz").read_text() == "stale"


@pytest.mark.parametrize("failed", [False, True])
def test_force_refreshes_exact_output_cache_with_legacy_retention(
    tmp_path, monkeypatch, source, failed
):
    fakes = _Fakes(monkeypatch)
    pair = (str(tmp_path / "x_R1.fastq.gz"), str(tmp_path / "x_R2.fastq.gz"))
    for path in pair:
        Path(path).write_text("stale")
    if failed:
        fakes.align_error = WGSExtractError("alignment failed")
        with pytest.raises(WGSExtractError, match="alignment failed"):
            realign.run(_args(source, force_extract=True))
        assert all(Path(path).read_text() == "@r\nA\n+\nI\n" for path in pair)
    else:
        realign.run(_args(source, force_extract=True))
        assert not any(Path(path).exists() for path in pair)
    assert fakes.extracted == [pair] and fakes.aligned == [pair]
    assert fakes.streamed == []


def test_failed_realign_retains_selected_workspace_fastqs_for_recovery(
    tmp_path, monkeypatch, source, caplog
):
    from wgsextract_cli.core.temp_directory import run_temporary_directory

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    fakes = _Fakes(monkeypatch)
    monkeypatch.setattr(
        realign, "collate_working_memory", lambda *_a, **_kw: 2 * 1024**4
    )
    fakes.align_error = RuntimeError("alignment interrupted")
    with (
        pytest.raises(RuntimeError, match="alignment interrupted"),
        run_temporary_directory(scratch),
    ):
        realign.run(_args(source, outdir=str(tmp_path / "out")))
    pair = fakes.extracted[0]
    workspace = Path(pair[0]).parent
    assert workspace.parent == scratch and workspace.exists()
    assert all(Path(path).read_text() == "@r\nA\n+\nI\n" for path in pair)
    assert str(workspace) in caplog.text and "--r1 and --r2" in caplog.text
    fakes.align_error = None
    with run_temporary_directory(scratch):
        realign.run(_args(source, r1=pair[0], r2=pair[1]))
    assert fakes.aligned == [pair, pair] and len(fakes.extracted) == 1
    assert all(Path(path).exists() for path in pair)


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
        realign.run(_args(source, keep_fastq=True))
    assert list(tmp_path.glob("*.fastq.gz")) == []
    assert fakes.aligned == []


def test_failed_alignment_keeps_fastqs_for_the_rerun(tmp_path, monkeypatch, source):
    fakes = _Fakes(monkeypatch)
    fakes.align_error = WGSExtractError("bwa alignment failed")
    with pytest.raises(WGSExtractError, match="bwa alignment failed"):
        realign.run(_args(source, keep_fastq=True))
    pair = (str(tmp_path / "x_R1.fastq.gz"), str(tmp_path / "x_R2.fastq.gz"))
    assert all(Path(p).is_file() for p in pair)

    fakes.align_error = None
    realign.run(_args(source))
    assert fakes.extracted == [pair] and fakes.aligned == [pair, pair]
    # Reused, not extracted by this run: left in place.
    assert all(Path(p).is_file() for p in pair)


@pytest.mark.parametrize("error", [WGSExtractError("boom"), KeyboardInterrupt()])
def test_stream_failure_leaves_no_fastqs_or_collate_spills(monkeypatch, source, error):
    fakes = _Fakes(monkeypatch)
    fakes.align_error = error
    with pytest.raises(type(error)):
        realign.run(_args(source))
    assert fakes.extracted == [] and len(fakes.streamed) == 1
    assert list(source.parent.glob("*.fastq.gz")) == []
    assert list(source.parent.glob(".collate_*")) == []


@needs_tools("bwa", "samtools")
@pytest.mark.parametrize("cram", [False, True], ids=["bam", "cram"])
@pytest.mark.parametrize("markdup", [False, True], ids=["auto-marker", "markdup"])
@pytest.mark.parametrize("aligner", ["bwa", "bwa-mem2"])
def test_paired_stream_body_matches_files_including_discarded_reads(
    tmp_path, genome_and_ref, monkeypatch, cram, markdup, aligner
):
    if realign.align_cmd.get_tool_path(aligner) is None:
        pytest.skip(f"{aligner} unavailable")
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 8 * 1024**3)
    monkeypatch.setattr(
        realign.align_cmd, "streaming_mapping_fits", lambda *_a, **_kw: True
    )
    genome, ref = genome_and_ref
    source, pairs = _old_alignment(tmp_path, genome)
    records = _view("view", source).splitlines()
    extra = []
    for flag in (0x100, 0x800):
        fields = records[0].split("\t")
        fields[1] = str(int(fields[1]) | flag)
        extra.append("\t".join(fields))
    extra.append(
        f"singleton\t73\told1\t1\t60\t100M\t*\t0\t0\t{genome[:100]}\t{'I' * 100}"
    )
    source = sam_to_bam(
        [*_view("view", "-H", source).splitlines(), *records, *extra],
        tmp_path / "old" / "filtered.bam",
    )
    input_ref = None
    if cram:
        input_ref = str(write_fasta(tmp_path / "old" / "encode.fa", {"old1": genome}))
        encoded = tmp_path / "old" / "filtered.cram"
        run_command(["samtools", "view", "-C", "-T", input_ref, "-o", encoded, source])
        run_command(["samtools", "index", encoded])
        source = str(encoded)
    if markdup:
        original = realign.align_cmd.get_tool_path
        monkeypatch.setattr(
            realign.align_cmd,
            "get_tool_path",
            lambda tool: None if tool == "samblaster" else original(tool),
        )
    for mode in ("files", "stream"):
        realign.run(
            _args(
                source,
                ref=ref,
                input_ref=input_ref,
                aligner=aligner,
                outdir=str(tmp_path / mode),
                keep_fastq=mode == "files",
            )
        )
    files = tmp_path / "files" / "filtered_aligned.bam"
    stream = tmp_path / "stream" / "filtered_aligned.bam"
    body = _view("view", stream)
    assert body == _view("view", files)
    assert len(body.splitlines()) == pairs * 2
    assert "singleton" not in body
    assert "-p " in _view("view", "-H", stream)
    assert ".fastq.gz" in _view("view", "-H", files)
    assert not list((tmp_path / "stream").glob("*.fastq.gz"))
    assert not list((tmp_path / "stream").glob(".collate_*"))


@needs_tools("bwa", "samtools")
@pytest.mark.parametrize("keep_fastq", [False, True], ids=["stream", "files"])
def test_single_end_reads_are_refused_before_extraction(
    tmp_path, genome_and_ref, monkeypatch, keep_fastq
):
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 8 * 1024**3)
    monkeypatch.setattr(
        realign.align_cmd, "streaming_mapping_fits", lambda *_a, **_kw: True
    )
    genome, ref = genome_and_ref
    lines = ["@HD\tVN:1.6\tSO:unsorted", f"@SQ\tSN:old1\tLN:{len(genome)}"]
    for i, flag in enumerate((0, 4, 0)):
        seq = genome[i * 100 : i * 100 + 100] if flag == 0 else "A" * 100
        pos = (i * 100 + 1, "100M") if flag == 0 else (0, "*")
        rname = "old1" if flag == 0 else "*"
        lines.append(
            f"se{i}\t{flag}\t{rname}\t{pos[0]}\t60\t{pos[1]}\t*\t0\t0\t{seq}\t{'I' * 100}"
        )
    source = sam_to_bam(lines, tmp_path / "old" / "single.bam")
    outdir = tmp_path / "out"
    with pytest.raises(WGSExtractError, match=r"3 of 3 sampled .* \(e\.g\. se0\)"):
        realign.run(_args(source, ref=ref, outdir=str(outdir), keep_fastq=keep_fastq))
    assert _visible(outdir) == []


@needs_tools("bwa", "samtools")
def test_streamed_cram_output_matches_file_path(tmp_path, genome_and_ref, monkeypatch):
    monkeypatch.setattr(
        realign.align_cmd, "streaming_mapping_fits", lambda *_a, **_kw: True
    )
    genome, ref = genome_and_ref
    source, _ = _old_alignment(tmp_path, genome)
    for mode in ("files", "stream"):
        realign.run(
            _args(
                source,
                ref=ref,
                aligner="bwa",
                format="CRAM",
                outdir=str(tmp_path / mode),
                keep_fastq=mode == "files",
            )
        )
    outputs = [tmp_path / mode / "x_aligned.cram" for mode in ("files", "stream")]
    assert _view("view", "-T", ref, str(outputs[0])) == _view(
        "view",
        "-T",
        ref,
        str(outputs[1]),
    )
    assert Path(str(outputs[1]) + ".crai").is_file()


@needs_tools("bwa", "samtools")
def test_native_stream_failure_removes_partial_output_and_collation(
    tmp_path,
    genome_and_ref,
    monkeypatch,
):
    monkeypatch.setattr(
        realign.align_cmd, "streaming_mapping_fits", lambda *_a, **_kw: True
    )
    genome, ref = genome_and_ref
    source, _ = _old_alignment(tmp_path, genome)
    monkeypatch.setattr(realign.align_cmd, "ensure_bwa_index", lambda _ref: None)
    outdir = tmp_path / "failure"
    with pytest.raises(WGSExtractError, match="bwa alignment failed"):
        realign.run(_args(source, ref=ref, outdir=str(outdir), aligner="bwa"))
    assert _visible(outdir) == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="the fake GPU host's tools are shell scripts"
)
def test_cleanup_problem_does_not_fail_a_finished_realign(
    tmp_path, monkeypatch, source, caplog, ram
):
    # A GPU realign always goes through FASTQs; run it on a fake GPU host.
    monkeypatch.setattr(sys, "platform", "linux")
    ram(128 * 10**9)
    host = FakeParabricks(tmp_path / "gpu", parabricks.DEFAULT_IMAGE)
    monkeypatch.setenv("PATH", f"{host.bin}{os.pathsep}{os.environ['PATH']}")
    fakes = _Fakes(monkeypatch)

    def align(args):
        fakes.align(args)
        Path(args.r1).unlink()

    monkeypatch.setattr(realign.align_cmd, "run", align)
    with caplog.at_level(logging.WARNING):
        realign.run(_args(source, gpu=True))
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
    args = _args(
        source,
        no_preflight=False,
        r1=None if extract else str(reads),
        keep_fastq=extract,
    )

    # Extraction's temp space is freed before alignment; its FASTQs are not:
    # max(10, 4 + 3 + 2) when extracting, 3 + 2 when not.
    with pytest.raises(WGSExtractError, match=f"needs ~{needed} GB.*--no-preflight"):
        realign.run(args)
    assert asked == [(str(tmp_path), needed)]
    assert fakes.extracted == [] and fakes.aligned == []


def test_stream_preflight_drops_only_the_fastqs_between_collate_and_sort(
    tmp_path,
    monkeypatch,
    source,
):
    estimates = {"Name": (10, 8), "Coord": (3, 2)}  # (temp GB, final GB)
    monkeypatch.setattr(
        realign,
        "get_free_space_needed",
        lambda _size, sort_type, **_k: estimates[sort_type],
    )
    asked = []
    monkeypatch.setattr(
        realign, "check_free_space", lambda path, gb: asked.append(gb) or False
    )
    for streaming in (False, True):
        with pytest.raises(WGSExtractError):
            realign._preflight_disk_space(
                str(source), str(tmp_path), not streaming, 100, streaming=streaming
            )
    # Files: max(10, 8 + 3 + 2). Streaming: collate's spills drain as the
    # sorter's grow, so max(10, 3 + 2).
    assert asked == [13, 10]


@pytest.mark.parametrize("requested", ["bwa", "bwa-mem2", "auto"])
def test_stream_overlap_without_ram_uses_files_without_changing_aligner(
    tmp_path, monkeypatch, source, requested
):
    fakes = _Fakes(monkeypatch)
    ref = tmp_path / "ref.fa"
    ref.write_text(">c\nA\n")
    monkeypatch.setattr(realign.bwa_mem2, "check_index", lambda *_a: None)
    monkeypatch.setattr(realign.bwa_mem2, "has_index", lambda *_a: True)
    monkeypatch.setattr(
        realign.bwa_mem2,
        "can_map",
        lambda *_a, **kw: kw.get("additional_ram_bytes", 0) == 0,
    )
    monkeypatch.setattr(realign.align_cmd, "get_tool_path", lambda *_a: "tool")
    monkeypatch.setattr(realign.align_cmd, "available_memory", lambda: 8 * 1024**3)
    monkeypatch.setattr(
        realign.align_cmd,
        "estimate_mapping_memory",
        lambda *_a, **_kw: MappingMemory(8 * 1024**3 - 32 * 1024**2, 0, 0, 0, 0),
    )
    checked = []
    monkeypatch.setattr(
        realign,
        "check_mapping_memory",
        lambda *_a, **kw: checked.append(kw["additional_ram_bytes"]),
    )
    chosen = []

    def align_files(args):
        fakes.align(args)
        chosen.append(
            realign.align_cmd.short_read_aligner(
                args.aligner, str(ref), reads=args._input_read_count
            )
        )

    monkeypatch.setattr(realign.align_cmd, "run", align_files)
    realign.run(_args(source, ref=str(ref), aligner=requested))
    assert len(fakes.extracted) == 1 and not fakes.streamed
    assert chosen == ["bwa" if requested == "bwa" else "bwa-mem2"]
    assert all(extra == 0 for extra in checked)


def test_stream_reserves_the_estimated_collate_bucket_for_reporting_and_mapping(
    tmp_path, monkeypatch, source, caplog
):
    fakes = _Fakes(monkeypatch)
    monkeypatch.setattr(realign, "indexed_read_count", lambda *_a: 1_000_000)
    estimates, disk = [], []
    for ram in (1024**4, 1):  # the bucket fits beside alignment, then doesn't
        monkeypatch.setattr(realign, "available_memory", lambda ram=ram: ram)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            realign.run(_args(source, aligner="bwa"))
        estimates.append(_preflight_gib(caplog, "memory"))
        disk.append(_preflight_gib(caplog, "disk"))
        (tmp_path / "x_aligned.bam").unlink()
    expected = realign.collate_working_memory(
        source, "2", "100M", records=1_000_000, max_record_bytes=400
    )
    assert len(fakes.streamed) == 1 and len(fakes.extracted) == 1
    assert fakes.streamed[0]["additional_ram_bytes"] == expected
    assert estimates[0] - estimates[1] == pytest.approx(expected / 1024**3, abs=0.01)
    assert disk[0] < disk[1]  # a stream writes no FASTQ files


@pytest.mark.parametrize("gib", [64, 2])
def test_whole_genome_input_streams_when_its_collate_bucket_fits_ram(
    tmp_path, monkeypatch, source, gib
):
    fakes = _Fakes(monkeypatch)
    with source.open("wb") as output:
        output.truncate(40 * 1024**3)
    monkeypatch.setattr(realign, "indexed_read_count", lambda *_a: 800_000_000)
    monkeypatch.setattr(realign, "calculate_bam_md5", lambda *_a: None)
    monkeypatch.setattr(realign, "resolve_reference", lambda *_a: None)
    monkeypatch.setattr(realign.align_cmd, "available_memory", lambda: gib * 1024**3)
    monkeypatch.setattr(realign, "available_memory", lambda: gib * 1024**3)
    realign.run(_args(source, aligner="bwa", threads="8", memory="2G"))
    if gib == 64:
        assert fakes.streamed and not fakes.extracted
    else:
        assert len(fakes.extracted) == 1 and not fakes.streamed
    assert len(fakes.aligned) == 1


def test_late_stream_memory_change_retries_file_extraction(
    tmp_path, monkeypatch, source, roomy_disk, caplog
):
    fakes = _Fakes(monkeypatch)
    monkeypatch.setattr(realign, "indexed_read_count", lambda *_a: 800_000_000)

    def unavailable(*_a, **_kw):
        raise realign.align_cmd.StreamingMemoryUnavailable("RAM changed")

    monkeypatch.setattr(realign.align_cmd, "align_bwa", unavailable)
    realign.run(_args(source, aligner="bwa", no_preflight=False))
    # Rechecked for FASTQ files kept until alignment, which streaming has none of.
    (_, streamed), (_, extracted) = _disk_preflights(caplog)
    assert streamed == 0 and extracted > 0
    assert len(fakes.extracted) == 1 and len(fakes.aligned) == 1
    assert not list(tmp_path.glob(".collate_*"))


@pytest.mark.parametrize("provided_fastqs", [False, True])
def test_realign_uses_actual_input_count_before_extraction_for_memory_and_disk(
    tmp_path, monkeypatch, source, provided_fastqs, roomy_disk, caplog
):
    _Fakes(monkeypatch)
    ref = tmp_path / "ref.fa"
    ref.write_text(">c\nA\n")
    Path(str(ref) + ".fai").write_text("c\t1\t3\t1\t2\n")
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

    def align(args, **_kwargs):
        budgets.append(("align", args._input_read_count))
        _write_alignment(args)

    monkeypatch.setattr(realign.align_cmd, "run", align)
    monkeypatch.setattr(realign.align_cmd, "align_bwa", align)
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
    assert budgets == [("memory", expected), ("align", expected)]
    # 800 million reads need room on disk; two reads in a 4-byte input don't.
    ((needed, _),) = _disk_preflights(caplog)
    assert (needed > 0) == (not provided_fastqs)


@pytest.mark.parametrize("no_preflight", [False, True])
@pytest.mark.parametrize("reads", [800_000_000, None])
def test_ultima_realign_counts_index_once_and_passes_through_unknown_counts(
    tmp_path, monkeypatch, source, no_preflight, reads, roomy_disk, caplog
):
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    calls = []

    def count(*_args):
        calls.append("idxstats")
        return reads

    monkeypatch.setattr(realign, "indexed_read_count", count)

    def align_ultima(args, *_a):
        calls.append(("align", args._input_read_count))
        Path(tmp_path, "x_aligned.bam").write_text("aligned")
        Path(tmp_path, "x_aligned.bam.bai").write_text("index")

    monkeypatch.setattr(realign.align_cmd, "align_ultima", align_ultima)
    realign.run(
        _args(source, platform="ultima", aligner="bwa", no_preflight=no_preflight)
    )
    assert calls == ["idxstats", ("align", reads)]
    # The count sizes the disk check; unknown, the 4-byte input's size does.
    preflights = _disk_preflights(caplog)
    if no_preflight:
        assert preflights == []
    else:
        ((needed, _),) = preflights
        assert (needed > 0) == (reads is not None)


@needs_tools("samtools")
def test_force_step_index_reindexes_an_alignment_its_aligner_indexed(
    tmp_path, monkeypatch, source
):
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(realign, "indexed_read_count", lambda *_args: 2)
    aligned = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:100", "r\t0\tchr1\t1\t60\t4M\t*\t0\t0\tACGT\tIIII"],
        tmp_path / "a.bam",
    )
    out = tmp_path / "x_aligned.bam"
    calls = []

    def align_ultima(*_args):
        calls.append("align")
        out.write_bytes(Path(aligned).read_bytes())
        Path(f"{out}.bai").write_text("the aligner's index")

    monkeypatch.setattr(realign.align_cmd, "align_ultima", align_ultima)
    args = {"platform": "ultima", "aligner": "bwa", "force_steps": None}
    realign.run(_args(source, **args))
    realign.run(_args(source, **args))
    assert calls == ["align"]

    realign.run(_args(source, **{**args, "force_steps": ["index"]}))
    assert calls == ["align"]
    assert Path(f"{out}.bai").read_bytes().startswith(b"BAI\x01")


# --- resuming after a crash ---------------------------------------------------


def _dying_tools(tmp_path, log):
    """PATH shims for the aligner, samtools and sambamba that log each call's
    subcommand and, when it matches the pattern in ``tmp_path/die``, SIGKILL
    the wgsextract run's process group and their own mid-call, as an OOM kill
    or power cut would (or send it ``tmp_path/signal``, such as INT for a
    Ctrl-C)."""
    import shutil
    import stat

    shims = tmp_path / "shims"
    shims.mkdir()
    for tool in ("bwa", "samtools", "sambamba"):
        real = shutil.which(tool)
        if real is None:
            continue
        script = shims / tool
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{tool} $1" >> "{log}"\n'
            f'die="$(cat "{tmp_path}/die" 2>/dev/null)"\n'
            f'if [ -n "$die" ] && [[ "{tool} $1" =~ ^($die)$ ]]; then\n'
            f'  rm -f "{tmp_path}/die"\n'
            # Half an output stream, then the crash.
            f'  "{real}" "$@" | head -c 4096\n'
            f'  sig="$(cat "{tmp_path}/signal" 2>/dev/null || echo 9)"\n'
            f'  kill -"$sig" -- "-$(cat "{tmp_path}/run.pid")"\n'
            "  kill -9 0\n"
            "fi\n"
            f'exec "{real}" "$@"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return shims


def _realign_cli(argv, shims, die=None, signal="9"):
    import os
    import subprocess
    import sys

    if die:
        (shims.parent / "die").write_text(die + "\n")
        (shims.parent / "signal").write_text(signal + "\n")
    env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}
    with subprocess.Popen(
        [sys.executable, "-m", "wgsextract_cli.main", "realign", *argv],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # its own process group, for the shim to kill
    ) as run:
        (shims.parent / "run.pid").write_text(str(run.pid))
        stdout, stderr = run.communicate()
    return subprocess.CompletedProcess(run.args, run.returncode, stdout, stderr)


def _records(bam):
    header = [h for h in _view("view", "-H", str(bam)).splitlines() if h[:3] != "@PG"]
    return header, _view("view", str(bam))


@pytest.mark.skipif(platform.system() == "Windows", reason="bash PATH shims")
@needs_tools("bwa", "samtools")
@pytest.mark.parametrize(
    "die, signal",
    [("bwa mem", "9"), ("bwa mem", "TERM"), ("(samtools|sambamba) index", "9"),
     ("(samtools|sambamba) index", "INT")],
)  # fmt: skip
def test_a_run_killed_mid_step_resumes_there_with_identical_output(
    tmp_path, genome_and_ref, die, signal
):
    genome, ref = genome_and_ref
    source, _ = _old_alignment(tmp_path, genome)
    log = tmp_path / "calls.log"
    shims = _dying_tools(tmp_path, log)

    def realign_to(outdir, die=None):
        log.write_text("")
        argv = ["--input", str(source), "--ref", ref, "--outdir", str(outdir),
                "--threads", "2", "--keep-fastq", "--sample", "Ann"]  # fmt: skip
        done = _realign_cli(argv, shims, die, signal)
        calls = [c for c in log.read_text().splitlines() if c != "samtools --version"]
        return done, calls

    clean, _ = realign_to(tmp_path / "clean")
    assert clean.returncode == 0, clean.stderr

    out = tmp_path / "out"
    killed, calls = realign_to(out, die=die)
    assert killed.returncode != 0
    assert any(re.fullmatch(die, call) for call in calls)
    bam = out / "x_aligned.bam"
    journal = json.loads((out / "x_aligned.bam.steps.json").read_text())
    assert journal["steps"]["extract"]["status"] == "complete"
    if signal != "9":
        # A run stopped by a signal reports it in the workflow's manifest.
        manifest = json.loads((out / "x_aligned.bam.manifest.json").read_text())
        assert manifest["status"] == "cancelled"
    assert not list(out.glob("*.steps.json.manifest.json"))

    resumed, calls = realign_to(out)
    assert resumed.returncode == 0, resumed.stderr
    assert "Running step extract" not in resumed.stderr
    # Nothing before the step it was killed in runs again.
    assert not {"samtools collate", "samtools fastq"} & set(calls)
    if die == "bwa mem":
        assert "Skipping step extract: complete" in resumed.stderr
        assert "Running step align: an earlier run was interrupted" in resumed.stderr
    else:
        assert "Skipping step align: complete" in resumed.stderr
        assert "bwa mem" not in calls
    assert any(re.fullmatch(die, call) for call in calls)
    assert _records(bam) == _records(tmp_path / "clean" / "x_aligned.bam")
    assert Path(str(bam) + ".bai").is_file()

    again, calls = realign_to(out)
    assert again.returncode == 0 and "bwa mem" not in calls
    assert not any(call.endswith(" index") for call in calls)


@pytest.mark.skipif(platform.system() == "Windows", reason="bash PATH shims")
@needs_tools("bwa", "samtools")
def test_truncated_outputs_and_changed_settings_redo_their_steps(
    tmp_path, genome_and_ref
):
    genome, ref = genome_and_ref
    source, _ = _old_alignment(tmp_path, genome)
    log = tmp_path / "calls.log"
    shims = _dying_tools(tmp_path, log)
    out = tmp_path / "out"
    bam = out / "x_aligned.bam"

    def realign_again(*extra):
        log.write_text("")
        argv = ["--input", str(source), "--ref", ref, "--outdir", str(out),
                "--threads", "2", "--keep-fastq", *extra]  # fmt: skip
        done = _realign_cli(argv, shims)
        assert done.returncode == 0, done.stderr
        return done.stderr, log.read_text().splitlines()

    realign_again()
    first = _records(bam)

    bam.write_bytes(bam.read_bytes()[:-100])
    stderr, calls = realign_again()
    assert "truncated or rewritten" in stderr
    assert "bwa mem" in calls and "samtools collate" not in calls
    assert _records(bam) == first

    stderr, calls = realign_again("--sample", "Bea")
    assert "settings changed: sample None -> 'Bea'" in stderr
    assert "bwa mem" in calls and "samtools collate" not in calls

    # A damaged FASTQ doesn't matter while the alignment made from it is
    # sound, but it is extracted again before it is aligned again.
    fastq = out / "x_R1.fastq.gz"
    fastq.write_bytes(fastq.read_bytes()[:-10])
    stderr, calls = realign_again("--sample", "Bea")
    assert "bwa mem" not in calls
    stderr, calls = realign_again("--sample", "Bea", "--force-step", "align")
    assert "Running step extract: x_R1.fastq.gz changed size" in stderr
    assert {"samtools collate", "bwa mem"} <= set(calls)


@needs_tools("bwa", "samtools")
def test_a_second_run_into_the_same_output_is_refused(tmp_path, genome_and_ref):
    from wgsextract_cli.core.steps import ResumePolicy, StepJournal

    genome, ref = genome_and_ref
    source, _ = _old_alignment(tmp_path, genome)
    out = tmp_path / "out"
    journal = out / "x_aligned.bam.steps.json"
    with StepJournal(journal, "realign", ResumePolicy()):
        with pytest.raises(WGSExtractError, match="Another wgsextract run"):
            realign.run(_args(source, ref=ref, outdir=str(out)))
    assert not (out / "x_aligned.bam").exists()
