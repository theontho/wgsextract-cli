import gzip
import hashlib
import logging
import os
import random
import re
from argparse import Namespace
from pathlib import Path

import pytest

from tests.fake_tools import python_tool, require_bare_name_fakes
from tests.htslib_helpers import (
    needs_tools,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
    write_read_pairs,
)
from wgsextract_cli.commands import align
from wgsextract_cli.core import alignment_memory, bwa_mem2
from wgsextract_cli.core.read_counts import fastq_input_reads
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.warnings import warn_alignment_sort_space

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


def _genome(tmp_path, pairs=200):
    """A 20 kb chr1 with ``pairs`` 100 bp read pairs from it."""
    rng = random.Random(7)
    genome = "".join(rng.choice("ACGT") for _ in range(20_000))
    (tmp_path / "ref").mkdir()
    ref = write_fasta(tmp_path / "ref" / "ref.fa", {"chr1": genome})
    r1, r2 = tmp_path / "reads_R1.fq", tmp_path / "reads_R2.fq"
    starts = [rng.randrange(0, len(genome) - 400) for _ in range(pairs)]
    write_read_pairs(genome, r1, r2, starts, frag=400)
    return str(ref), str(r1), str(r2)


def _align(ref, r1, r2, out, *options):
    run_wgsextract(
        "align", "--r1", r1, "--r2", r2, "--ref", ref, "--outdir", str(out),
        "--threads", "2", *options,
    )  # fmt: skip
    (bam,) = out.glob("*_aligned.bam")
    return str(bam)


def _header(bam):
    return run_command(["samtools", "view", "-H", bam], capture_output=True).stdout


def _mem2_index(ref):
    """The files beside ``ref`` but its (persistent) build lock."""
    files = Path(ref).parent.glob(Path(ref).name + ".*")
    return sorted(p.name for p in files if not p.name.endswith(".lock.guard"))


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
    tmp_path, monkeypatch, ram, installed, index, memory, expected
):
    ref = _reference(tmp_path, bases=3_000_000_000, index=index)
    monkeypatch.setattr(
        align, "get_tool_path", lambda tool: tool if installed else None
    )
    ram(128 * GB, memory)
    assert align.short_read_aligner("auto", ref) == expected
    assert align.short_read_aligner("bwa", ref) == "bwa"


def test_auto_memory_budget_counts_sort_threads_and_available_not_total(
    tmp_path, monkeypatch, ram
):
    ref = _reference(tmp_path, bases=3_000_000_000, index=True)
    low = bwa_mem2.mapping_memory(ref, threads="6", memory="256M")
    high = bwa_mem2.mapping_memory(ref, threads="6", memory="2G")
    assert high > low
    ram(128 * GB, low)
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    assert (
        align.short_read_aligner("auto", ref, threads="6", memory="256M") == "bwa-mem2"
    )
    assert align.short_read_aligner("auto", ref, threads="6", memory="2G") == "bwa-mem2"
    assert align.short_read_aligner("auto", ref, threads="48", memory="256M") == "bwa"


def _mem2_budget_between_256m_and_512m(ref, reads):
    def need(memory):
        return alignment_memory.estimate_mapping_memory(
            ref, "bwa-mem2", threads="2", memory=memory, reads=reads
        ).total

    return (need("256M") + need("512M")) // 2


def _preflight_memory_warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.getMessage().startswith("Preflight")
    ]


@needs_tools("bwa-mem2", "samtools")
@pytest.mark.parametrize("requested", ["auto", "bwa-mem2"])
def test_mem2_sorts_in_256m_per_thread_and_budgets_ram_for_that(
    tmp_path, ram, caplog, requested
):
    ref, r1, r2 = _genome(tmp_path)
    run_wgsextract("ref", "index", "--ref", ref, "--aligner", "bwa-mem2")
    # Enough RAM for MEM2 sorting in 256M a thread, not in the 512M asked for:
    # auto still takes it, and neither the guard nor the preflight objects.
    ram(64 * GB, _mem2_budget_between_256m_and_512m(ref, 400))
    caplog.clear()
    caplog.set_level(logging.INFO)

    bam = _align(ref, r1, r2, tmp_path / "out", "--aligner", requested, "-m", "512M")

    header = _header(bam)
    assert "\tID:bwa-mem2\t" in header
    (sort,) = [line for line in header.splitlines() if "CL:samtools sort" in line]
    # contract: the sort budget is what the memory guard counted
    assert " -m 256M " in sort
    assert not _preflight_memory_warnings(caplog)


@needs_tools("bwa-mem2", "samtools")
def test_realign_budgets_mem2_ram_for_its_256m_sort(tmp_path, ram, caplog):
    ref, r1, r2 = _genome(tmp_path)
    run_wgsextract("ref", "index", "--ref", ref, "--aligner", "bwa-mem2")
    ram(64 * GB)
    source = _align(ref, r1, r2, tmp_path / "first", "--aligner", "bwa")
    ram(64 * GB, _mem2_budget_between_256m_and_512m(ref, 400))
    caplog.clear()
    caplog.set_level(logging.INFO)

    run_wgsextract(
        "realign", "--input", source, "--ref", ref, "--outdir", str(tmp_path / "out"),
        "--aligner", "bwa-mem2", "--threads", "2", "--memory", "512M",
    )  # fmt: skip

    (bam,) = (tmp_path / "out").glob("*_aligned.bam")
    assert "\tID:bwa-mem2\t" in _header(str(bam))
    assert not _preflight_memory_warnings(caplog)


@pytest.mark.parametrize(
    ("tool", "suffixes"),
    [("bwa", (".bwt", ".sa", ".pac")), ("bwa-mem2", (".0123", ".bwt.2bit.64", ".pac"))],
)
def test_mapping_estimate_uses_resident_files_and_index_length_without_fai(
    tmp_path, tool, suffixes
):
    ref = _reference(tmp_path, bases=3_000_000_000)
    sizes = [129, 513, 1025]
    for suffix, size in zip(suffixes, sizes, strict=True):
        Path(ref + suffix).write_bytes(b"x" * size)
    Path(ref + ".ann").write_text("3000000000 1 11\n")
    before = alignment_memory.estimate_mapping_memory(
        ref, tool, threads="6", memory="0.5G"
    )
    assert before.index_resident == sum(sizes)
    Path(ref + ".fai").unlink()
    after = alignment_memory.estimate_mapping_memory(
        ref, tool, threads="6", memory="512M"
    )
    assert before == after
    Path(ref + suffixes[0]).write_bytes(b"x" * (sizes[0] + 4096))
    grown = alignment_memory.estimate_mapping_memory(
        ref, tool, threads="6", memory="512M"
    )
    assert grown.total == after.total + 4096


def test_samblaster_allowance_only_applies_when_that_stage_runs(tmp_path, ram):
    ref = _reference(tmp_path, bases=3_000_000_000, index=True)
    without = alignment_memory.estimate_mapping_memory(
        ref, "bwa-mem2", threads="2", memory="100M", samblaster=False
    )
    ram(128 * GB, without.total)
    alignment_memory.check_mapping_memory(
        ref, "bwa-mem2", threads="2", memory="100M", samblaster=False
    )
    with pytest.raises(WGSExtractError, match="input read count"):
        alignment_memory.check_mapping_memory(
            ref, "bwa-mem2", threads="2", memory="100M", samblaster=True
        )


@needs_tools("bwa", "bwa-mem2", "samtools")
@pytest.mark.parametrize("tool", ["bwa", "bwa-mem2"])
def test_low_available_memory_refuses_mem2_but_bwa_warns_and_runs(
    tmp_path, ram, caplog, tool
):
    ref, r1, r2 = _genome(tmp_path)
    before = _mem2_index(ref)
    ram(64 * GB, 1024**2)
    out = tmp_path / "out"
    if tool == "bwa-mem2":
        with pytest.raises(WGSExtractError, match="--memory.*--aligner bwa"):
            _align(ref, r1, r2, out, "--aligner", tool)
        # Refused before building an index or writing anything.
        assert _mem2_index(ref) == before
        assert not out.exists() or not list(out.iterdir())
    else:
        bam = _align(ref, r1, r2, out, "--aligner", tool)
        assert "\tID:bwa\t" in _header(bam)
        assert "Continuing with bwa" in caplog.text
        assert "--memory" in caplog.text


@pytest.mark.parametrize(
    ("tool", "available_gib", "refused", "warned"),
    [
        ("bwa", 17.5, False, True),
        ("bwa", 18.5, False, False),
        ("bwa-mem2", 28, True, False),
        ("bwa-mem2", 30, False, False),
    ],
)
def test_full_human_budget_accounts_for_final_h2_paged_working_set(
    tmp_path, ram, caplog, tool, available_gib, refused, warned
):
    ref = _reference(tmp_path, bases=3_217_346_917)
    ram(64 * GB, int(available_gib * GB))
    kwargs = {
        "threads": "6",
        "memory": "256M",
        "samblaster": True,
        "reads": 679_639_896,
    }
    if refused:
        with pytest.raises(WGSExtractError, match="--aligner bwa"):
            alignment_memory.check_mapping_memory(ref, tool, **kwargs)
    else:
        alignment_memory.check_mapping_memory(ref, tool, **kwargs)
    assert ("Continuing with bwa" in caplog.text) is warned


def test_default_bwa_sort_budget_warns_below_18g_available(tmp_path, ram, caplog):
    from wgsextract_cli.core.utils import get_resource_defaults

    ref = _reference(tmp_path, bases=3_217_346_917)
    ram(32 * GB, 17 * GB)
    threads, memory = get_resource_defaults("6", None)
    alignment_memory.check_mapping_memory(
        ref, "bwa", threads=threads, memory=memory, samblaster=True
    )
    assert "Continuing with bwa" in caplog.text


def test_small_input_does_not_inherit_full_genome_transient_allowance(tmp_path, ram):
    ref = _reference(tmp_path, bases=20_000, index=True)
    ram(16 * GB, 4 * GB)
    alignment_memory.check_mapping_memory(
        ref, "bwa-mem2", threads="2", memory="100M", samblaster=True, reads=300
    )


def _bam_on(ref, path, length):
    """A sorted, indexed BAM of one read pair on ``ref``'s chr1 of ``length``
    bases."""
    seq = "ACGT" * 25
    return sam_to_bam(
        [
            f"@SQ\tSN:chr1\tLN:{length}",
            "@RG\tID:rg\tSM:s\tPL:ILLUMINA",
            f"p\t99\tchr1\t1\t60\t100M\t=\t201\t300\t{seq}\t{'I' * 100}\tRG:Z:rg",
            f"p\t147\tchr1\t201\t60\t100M\t=\t1\t-300\t{seq}\t{'I' * 100}\tRG:Z:rg",
        ],
        path,
    )


def _fastqs(folder):
    return [p for p in folder.rglob("*") if re.search(r"\.f(ast)?q(\.gz)?$", p.name)]


@needs_tools("samtools")
@pytest.mark.parametrize("reference_source", ["fasta", "library", "configured"])
def test_realign_refuses_mem2_memory_before_extracting(
    tmp_path, monkeypatch, ram, reference_source
):
    from wgsextract_cli.core.config import settings

    library = tmp_path / "library"
    library.mkdir()
    ref = _reference(library, bases=2_000_000_000, index=True)
    # MEM2 maps its index into RAM; a sparse one is as big without the disk.
    os.truncate(ref + ".bwt.2bit.64", 8 * GB)
    # A small genome the input wasn't aligned to: it would fit.
    (library / "hg19.fa").write_text(">chr2\nA\n")
    (library / "hg19.fa.fai").write_text("chr2\t1\t6\t1\t2\n")
    small = alignment_memory.estimate_mapping_memory(
        str(library / "hg19.fa"), "bwa-mem2", threads="6", memory="256M", reads=2
    )
    ram(64 * GB, small.total + GB)
    monkeypatch.setitem(settings, "reference_library", str(library))
    out = tmp_path / "out"
    source = _bam_on(ref, tmp_path / "source.bam", 2_000_000_000)
    where = {"fasta": ["--ref", ref], "library": ["--ref", str(library)]}

    with pytest.raises(WGSExtractError, match="bwa-mem2 alignment is estimated") as e:
        run_wgsextract(
            "realign", "--input", source, *where.get(reference_source, []),
            "--outdir", str(out), "--aligner", "bwa-mem2",
            "--threads", "6", "--memory", "512M",
        )  # fmt: skip

    # The input's own reference was checked, with MEM2's 256M sort budget.
    needed = alignment_memory.estimate_mapping_memory(
        ref, "bwa-mem2", threads="6", memory="256M", reads=2
    )
    total = float(re.search(r"need ([\d.]+) GiB", str(e.value))[1]) * GB
    # A 512M sort budget would add 1.5 GiB over six threads.
    assert needed.total - 0.01 * GB <= total < needed.total + GB
    assert not _fastqs(tmp_path)


def test_same_reference_budget_grows_with_sampled_fastq_input(tmp_path):
    ref = _reference(tmp_path, bases=3_217_346_917)
    member = gzip.compress(b"@r\nACGT\n+\nIIII\n" * 100)
    fastq = tmp_path / "reads.fq.gz"
    fastq.write_bytes(member)
    small = alignment_memory.estimate_mapping_memory(
        ref, "bwa", reads=fastq_input_reads(fastq)
    )
    fastq.write_bytes(member * 10)
    large = alignment_memory.estimate_mapping_memory(
        ref, "bwa", reads=fastq_input_reads(fastq)
    )
    assert large.index_resident == small.index_resident
    assert large.samblaster == small.samblaster * 10
    assert large.overhead > small.overhead
    assert large.total > small.total


def test_input_count_scales_memory_auto_selection_and_sort_disk(
    tmp_path, monkeypatch, ram, caplog
):
    ref = _reference(tmp_path, bases=3_217_346_917, index=True)
    count = 679_639_896
    low = alignment_memory.estimate_mapping_memory(
        ref, "bwa-mem2", reads=count, threads="6", memory="256M"
    )
    high = alignment_memory.estimate_mapping_memory(
        ref, "bwa-mem2", reads=count * 2, threads="6", memory="256M"
    )
    assert high.samblaster == 2 * low.samblaster
    assert high.total > low.total
    ram(128 * GB, low.total)
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    kwargs = {"threads": "6", "memory": "256M"}
    assert align.short_read_aligner("auto", ref, reads=count, **kwargs) == "bwa-mem2"
    assert align.short_read_aligner("auto", ref, reads=count * 2, **kwargs) == "bwa"
    caplog.set_level(logging.INFO)
    warn_alignment_sort_space(tmp_path, count)
    warn_alignment_sort_space(tmp_path, count * 2)
    low_disk, high_disk = (
        float(m[1])
        for m in re.finditer(r"sort temporary disk estimate: ([\d.]+) GiB", caplog.text)
    )
    # A 30x run of this many reads spilled 74 bytes a read (47 GiB) to sort.
    assert 47 <= low_disk < 60
    assert high_disk == pytest.approx(2 * low_disk, rel=0.01)


def test_h2_read_count_calibration_is_about_18_gib_for_bwa(tmp_path):
    ref = _reference(tmp_path, bases=3_217_346_917)
    budget = alignment_memory.estimate_mapping_memory(
        ref, "bwa", reads=679_639_896, threads="6", memory="256M"
    )
    assert 18 <= budget.total / GB < 18.5
    assert budget.samblaster >= 6_015_033_344


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


def test_missing_index_without_memory_to_build_says_what_to_do(
    tmp_path, monkeypatch, ram
):
    ref = _reference(tmp_path, bases=3_000_000_000)
    ram(32 * GB)
    with pytest.raises(WGSExtractError, match="needs 78 GB of memory.*--aligner bwa"):
        bwa_mem2.ensure_index(ref, download=True)

    monkeypatch.setattr(
        bwa_mem2, "prebuilt_index", lambda _ref: bwa_mem2.PrebuiltIndex("x", "u", {})
    )
    with pytest.raises(WGSExtractError, match="ref index --aligner bwa-mem2"):
        bwa_mem2.ensure_index(ref, download=False)


@needs_tools("samtools", "bwa-mem2")
def test_align_indexes_a_bare_fasta_for_mem2(tmp_path, ram):
    ref, r1, r2 = _genome(tmp_path)
    ram(64 * GB)
    Path(ref + ".fai").unlink()

    bam = _align(ref, r1, r2, tmp_path / "out", "--aligner", "bwa-mem2")

    assert Path(ref + ".fai").read_text().split("\t")[:2] == ["chr1", "20000"]
    assert bwa_mem2.has_index(ref)
    assert "\tID:bwa-mem2\t" in _header(bam)


_FAKE_MEM2_INDEX = """\
import sys
assert sys.argv[1:3] == ["index", "-p"], sys.argv
for suffix in {suffixes!r}:
    open(sys.argv[3] + suffix, "w").write("index")
"""


@pytest.mark.parametrize("bases", [2**31 - 1, 2**31, 3_217_346_917])
@pytest.mark.parametrize("entry", ["build_index", "ensure_index"])
def test_large_mem2_index_can_be_built_on_every_platform(
    tmp_path, monkeypatch, ram, bases, entry
):
    require_bare_name_fakes()
    ref = _reference(tmp_path, bases=bases)
    ram(128 * GB)
    # Building a human index takes an hour: a stand-in writes its files.
    python_tool(
        tmp_path / "bin",
        "bwa-mem2",
        _FAKE_MEM2_INDEX.format(suffixes=list(bwa_mem2.INDEX_SUFFIXES)),
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    bwa_mem2.check_index(ref)
    if entry == "ensure_index":
        bwa_mem2.ensure_index(ref, download=False)
    else:
        bwa_mem2.build_index(ref)
    assert bwa_mem2.has_index(ref)
    assert not Path(ref + ".bwa-mem2.pending").exists()


@needs_tools("bwa-mem2")
@pytest.mark.parametrize("metadata", [None, "chr1\t10\t6\t60\t61\nchr2\n"])
@pytest.mark.parametrize("entry", ["build_index", "check_index", "ensure_index"])
def test_unknown_or_incomplete_length_cannot_bypass_build_memory_preflight(
    tmp_path, ram, metadata, entry
):
    ref = _reference(tmp_path)
    fai = Path(ref + ".fai")
    if metadata is None:
        fai.unlink()
    else:
        fai.write_text(metadata)
    ram(8 * GB)
    before = _mem2_index(ref)
    with pytest.raises(WGSExtractError, match="reference length.*samtools faidx"):
        if entry == "ensure_index":
            bwa_mem2.ensure_index(ref, download=False)
        else:
            getattr(bwa_mem2, entry)(ref)
    assert _mem2_index(ref) == before


@needs_tools("bwa-mem2")
def test_ann_length_can_protect_large_build_without_a_fai(tmp_path, ram):
    ref = _reference(tmp_path, bases=3_217_346_917)
    Path(ref + ".fai").unlink()
    Path(ref + ".ann").write_text("3217346917 3366 11\n")
    ram(8 * GB)
    with pytest.raises(WGSExtractError, match="needs.*memory"):
        bwa_mem2.ensure_index(ref, download=False)
    assert _mem2_index(ref) == ["ref.fa.ann"]


@needs_tools("samtools", "bwa-mem2")
def test_ref_index_builds_the_mem2_index(tmp_path):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 1500})
    Path(str(ref) + ".fai").unlink()

    run_wgsextract("ref", "index", "--ref", str(ref), "--aligner", "bwa-mem2")

    assert (tmp_path / "ref.fa.fai").is_file()
    assert (tmp_path / "ref.dict").is_file()
    assert bwa_mem2.has_index(str(ref))


@needs_tools("bwa-mem2")
def test_large_existing_index_remains_usable(tmp_path, ram):
    ref = _reference(tmp_path, bases=2**31, index=True)
    ram(16 * GB)
    bwa_mem2.check_index(ref)
    bwa_mem2.ensure_index(ref, download=False)
    # Kept as it is, not rebuilt.
    assert all(
        Path(ref + suffix).read_text() == "x" for suffix in bwa_mem2.INDEX_SUFFIXES
    )


def test_prebuilt_index_downloads_and_is_checked(tmp_path, monkeypatch):
    ref = _reference(tmp_path, bases=3_217_346_917)
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
@pytest.mark.parametrize("memory", ["100M", "512M"])
def test_bwa_mem2_aligns_like_bwa(tmp_path, ram, memory):
    ref, r1, r2 = _genome(tmp_path)
    ram(64 * GB)
    positions = {}
    for aligner in ("bwa", "bwa-mem2"):
        bam = _align(
            ref, r1, r2, tmp_path / aligner, "--aligner", aligner, "-m", memory
        )
        assert f"ID:{aligner}\t" in _header(bam)
        reads = run_command(["samtools", "view", bam], capture_output=True)
        positions[aligner] = sorted(
            tuple(line.split("\t")[:6]) for line in reads.stdout.splitlines()
        )
    assert len(positions["bwa"]) == 400
    assert positions["bwa"] == positions["bwa-mem2"]


@needs_tools("samtools")
def test_realign_checks_the_bwa_mem2_index_before_extracting(tmp_path, ram):
    # BAM holds contigs below 2**31 bases.
    ref = _reference(tmp_path, bases=2_000_000_000)
    source = _bam_on(ref, tmp_path / "x.bam", 2_000_000_000)
    ram(32 * GB)
    with pytest.raises(WGSExtractError, match="no bwa-mem2 index"):
        run_wgsextract(
            "realign", "--input", source, "--ref", ref, "--outdir", str(tmp_path),
            "--aligner", "bwa-mem2", "--no-preflight", "--keep-fastq",
        )  # fmt: skip
    assert not _fastqs(tmp_path)
