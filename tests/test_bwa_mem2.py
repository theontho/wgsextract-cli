import hashlib
import random
from argparse import Namespace

import pytest

from tests.htslib_helpers import needs_tools, write_fasta
from wgsextract_cli.commands import align
from wgsextract_cli.core import bwa_mem2
from wgsextract_cli.core.utils import WGSExtractError, run_command

GB = 1024**3


def _reference(tmp_path, bases=1000, index=False):
    # These tests read reference metadata, not sequence. Model large genomes in
    # the .fai without allocating or writing gigabytes of dummy bases.
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nA\n")
    (tmp_path / "ref.fa.fai").write_text(f"chr1\t{bases}\t6\t{bases}\t{bases + 1}\n")
    if index:
        for suffix in bwa_mem2.INDEX_SUFFIXES:
            (tmp_path / f"ref.fa{suffix}").write_text("x")
    return str(ref)


@pytest.mark.parametrize(
    ("installed", "index", "memory", "expected"),
    [
        (False, True, 64 * GB, "bwa"),
        (True, True, 64 * GB, "bwa-mem2"),
        (True, True, 1 * GB, "bwa"),  # the index doesn't fit in memory
        (True, False, 64 * GB, "bwa"),  # no index: ref index sets it up
    ],
)
def test_auto_takes_bwa_mem2_when_its_index_is_there_and_fits(
    tmp_path, monkeypatch, installed, index, memory, expected
):
    ref = _reference(tmp_path, bases=3_000_000_000, index=index)
    monkeypatch.setattr(
        align, "get_tool_path", lambda tool: tool if installed else None
    )
    monkeypatch.setattr(bwa_mem2, "_total_memory", lambda: memory)
    assert align.short_read_aligner("auto", ref) == expected
    assert align.short_read_aligner("bwa", ref) == "bwa"


def test_select_aligner_accepts_bwa_mem2_for_short_reads():
    args = Namespace(
        aligner="bwa-mem2", platform="dnbseq", long_read=False, preset=None
    )
    assert align.select_aligner(args) == "bwa-mem2"


def _write_dict(tmp_path, rows):
    (tmp_path / "ref.dict").write_text(
        "@HD\tVN:1.0\n"
        + "".join(f"@SQ\tSN:{n}\tLN:{ln}\tM5:{m5}\tUR:file:x\n" for n, ln, m5 in rows)
    )


def test_contig_fingerprint_covers_names_lengths_and_md5s(tmp_path):
    ref = _reference(tmp_path)
    rows = [("chr1", 1000, "a" * 32), ("chr2", 500, "b" * 32)]
    _write_dict(tmp_path, rows)
    expected = "".join(f"{n}\t{ln}\t{m5}\n" for n, ln, m5 in rows)
    assert (
        bwa_mem2.contig_fingerprint(ref)
        == hashlib.sha256(expected.encode()).hexdigest()
    )

    # Same names and lengths, different sequence (e.g. a masked chrY PAR).
    _write_dict(tmp_path, [("chr1", 1000, "a" * 32), ("chr2", 500, "c" * 32)])
    assert (
        bwa_mem2.contig_fingerprint(ref)
        != hashlib.sha256(expected.encode()).hexdigest()
    )


def test_hs38dh_has_a_prebuilt_index():
    """The pinned fingerprint is hs38DH's: computed from its .dict, which is
    identical to Broad's Homo_sapiens_assembly38.dict (3366 contigs)."""
    (prebuilt,) = bwa_mem2.PREBUILT_INDEXES.values()
    assert set(prebuilt.files) == set(bwa_mem2.INDEX_SUFFIXES)
    assert all(len(sha) == 64 for _size, sha in prebuilt.files.values())


def test_missing_index_without_memory_to_build_says_what_to_do(tmp_path, monkeypatch):
    ref = _reference(tmp_path, bases=3_000_000_000)
    monkeypatch.setattr(bwa_mem2, "_total_memory", lambda: 32 * GB)
    monkeypatch.setattr(bwa_mem2, "prebuilt_index", lambda _ref: None)
    with pytest.raises(WGSExtractError, match="needs 78 GB of memory.*--aligner bwa"):
        bwa_mem2.ensure_index(ref, download=True)

    monkeypatch.setattr(
        bwa_mem2, "prebuilt_index", lambda _ref: bwa_mem2.PrebuiltIndex("x", "u", {})
    )
    with pytest.raises(WGSExtractError, match="ref index --aligner bwa-mem2"):
        bwa_mem2.ensure_index(ref, download=False)


def test_index_is_built_when_it_fits_in_memory(tmp_path, monkeypatch):
    ref = _reference(tmp_path)
    monkeypatch.setattr(bwa_mem2, "prebuilt_index", lambda _ref: None)
    commands = []
    monkeypatch.setattr(bwa_mem2, "run_command", lambda cmd, **_k: commands.append(cmd))
    bwa_mem2.ensure_index(ref, download=True)
    assert commands == [["bwa-mem2", "index", ref]]


def test_prebuilt_index_downloads_and_is_checked(tmp_path, monkeypatch):
    ref = _reference(tmp_path)
    content = {s: f"index{s}".encode() for s in bwa_mem2.INDEX_SUFFIXES}
    prebuilt = bwa_mem2.PrebuiltIndex(
        "test",
        "https://example.org/ref.fasta",
        {s: (len(c), hashlib.sha256(c).hexdigest()) for s, c in content.items()},
    )
    monkeypatch.setattr(bwa_mem2, "prebuilt_index", lambda _ref: prebuilt)
    corrupt = {".pac"}

    def fake_download(url, dest):
        suffix = url.removeprefix(prebuilt.base_url)
        data = content[suffix]
        with open(dest, "wb") as f:
            f.write(data[::-1] if suffix in corrupt else data)
        return True

    monkeypatch.setattr(bwa_mem2, "download_file", fake_download)
    with pytest.raises(WGSExtractError, match="didn't download intact"):
        bwa_mem2.ensure_index(ref, download=True)
    assert not (tmp_path / "ref.fa.pac").exists()

    corrupt.clear()
    bwa_mem2.ensure_index(ref, download=True)
    assert bwa_mem2.has_index(ref)


@needs_tools("bwa", "bwa-mem2", "samtools")
def test_bwa_mem2_aligns_like_bwa(tmp_path, monkeypatch):
    rng = random.Random(7)
    genome = "".join(rng.choice("ACGT") for _ in range(20_000))
    ref = str(write_fasta(tmp_path / "ref.fa", {"chr1": genome}))
    comp = str.maketrans("ACGT", "TGCA")
    with open(tmp_path / "r1.fq", "w") as r1, open(tmp_path / "r2.fq", "w") as r2:
        for i in range(200):
            start = rng.randrange(0, len(genome) - 400)
            frag = genome[start : start + 400]
            r1.write(f"@p{i}\n{frag[:100]}\n+\n{'I' * 100}\n")
            r2.write(f"@p{i}\n{frag[-100:].translate(comp)[::-1]}\n+\n{'I' * 100}\n")
    monkeypatch.setattr(bwa_mem2, "_total_memory", lambda: 64 * GB)
    positions = {}
    for aligner in ("bwa", "bwa-mem2"):
        outdir = tmp_path / aligner
        align.run(
            Namespace(
                aligner=aligner,
                r1=str(tmp_path / "r1.fq"),
                r2=str(tmp_path / "r2.fq"),
                ref=ref,
                input=None,
                outdir=str(outdir),
                format="BAM",
                threads="2",
                memory="100M",
                platform="illumina",
                long_read=False,
                preset=None,
                gpu=False,
                sample=None,
            )
        )
        (bam,) = (str(p) for p in outdir.glob("*_aligned.bam"))
        header = run_command(["samtools", "view", "-H", bam], capture_output=True)
        assert f"ID:{aligner}\t" in header.stdout
        reads = run_command(["samtools", "view", bam], capture_output=True)
        positions[aligner] = sorted(
            tuple(line.split("\t")[:6]) for line in reads.stdout.splitlines()
        )
    assert len(positions["bwa"]) == 400
    assert positions["bwa"] == positions["bwa-mem2"]


def test_realign_checks_the_bwa_mem2_index_before_extracting(tmp_path, monkeypatch):
    from wgsextract_cli.commands import realign

    ref = _reference(tmp_path, bases=3_000_000_000)
    source = tmp_path / "x.bam"
    source.write_bytes(b"data")
    extracted = []
    monkeypatch.setattr(realign, "alignments_to_fastq", extracted.append)
    monkeypatch.setattr(bwa_mem2, "_total_memory", lambda: 32 * GB)
    monkeypatch.setattr(bwa_mem2, "prebuilt_index", lambda _ref: None)
    args = Namespace(
        input=str(source),
        ref=ref,
        outdir=str(tmp_path),
        threads=None,
        memory=None,
        r1=None,
        r2=None,
        force_extract=False,
        no_preflight=True,
        keep_fastq=False,
        aligner="bwa-mem2",
        platform="illumina",
        long_read=False,
        preset=None,
    )
    with pytest.raises(WGSExtractError, match="no bwa-mem2 index"):
        realign.run(args)
    assert extracted == []
