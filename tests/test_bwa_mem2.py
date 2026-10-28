import gzip
import hashlib
import random
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import needs_tools, write_fasta
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
    monkeypatch.setattr(bwa_mem2, "available_memory", lambda: memory)
    assert align.short_read_aligner("auto", ref) == expected
    assert align.short_read_aligner("bwa", ref) == "bwa"


def test_auto_memory_budget_counts_sort_threads_and_available_not_total(
    tmp_path, monkeypatch
):
    ref = _reference(tmp_path, bases=3_000_000_000, index=True)
    low = bwa_mem2.mapping_memory(ref, threads="6", memory="256M")
    high = bwa_mem2.mapping_memory(ref, threads="6", memory="2G")
    assert high > low
    monkeypatch.setattr(
        alignment_memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=128 * GB, available=low),
    )
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    assert (
        align.short_read_aligner("auto", ref, threads="6", memory="256M") == "bwa-mem2"
    )
    assert align.short_read_aligner("auto", ref, threads="6", memory="2G") == "bwa"
    assert align.short_read_aligner("auto", ref, threads="48", memory="256M") == "bwa"


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


def test_samblaster_allowance_only_applies_when_that_stage_runs(tmp_path, monkeypatch):
    ref = _reference(tmp_path, bases=3_000_000_000, index=True)
    without = alignment_memory.estimate_mapping_memory(
        ref, "bwa-mem2", threads="2", memory="100M", samblaster=False
    )
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: without.total)
    alignment_memory.check_mapping_memory(
        ref, "bwa-mem2", threads="2", memory="100M", samblaster=False
    )
    with pytest.raises(WGSExtractError, match="input read count"):
        alignment_memory.check_mapping_memory(
            ref, "bwa-mem2", threads="2", memory="100M", samblaster=True
        )


@pytest.mark.parametrize("tool", ["bwa", "bwa-mem2"])
def test_low_available_memory_refuses_mem2_but_bwa_warns_and_runs(
    tmp_path, monkeypatch, caplog, tool
):
    ref = _reference(tmp_path, bases=3_000_000_000, index=True)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: GB)
    setup = align._AlignSetup("6", "512M", str(tmp_path), ref, "sample")
    monkeypatch.setattr(align, "_prepare_alignment", lambda *_: setup)
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(align, "ensure_bwa_index", lambda *_: None)
    monkeypatch.setattr(align, "ensure_bwa_alt_file", lambda *_: None)
    monkeypatch.setattr(align, "print_warning", lambda *_a, **_k: None)
    pipelines = []
    monkeypatch.setattr(
        align, "run_streamed_alignment", lambda *_a, **_k: pipelines.append(True)
    )
    args = Namespace(aligner=tool, r1="r1.fq", r2="r2.fq", format="BAM")
    if tool == "bwa-mem2":
        monkeypatch.setattr(
            bwa_mem2,
            "ensure_index",
            lambda *_a, **_k: pytest.fail("index work started"),
        )
        with pytest.raises(WGSExtractError, match="--memory.*--aligner bwa"):
            align.align_bwa(args)
        assert not pipelines
    else:
        align.align_bwa(args)
        assert pipelines == [True]
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
    tmp_path, monkeypatch, caplog, tool, available_gib, refused, warned
):
    ref = _reference(tmp_path, bases=3_217_346_917)
    monkeypatch.setattr(
        alignment_memory, "available_memory", lambda: int(available_gib * GB)
    )
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


def test_default_bwa_sort_budget_warns_below_18g_available(
    tmp_path, monkeypatch, caplog
):
    from wgsextract_cli.core.utils import get_resource_defaults

    ref = _reference(tmp_path, bases=3_217_346_917)
    monkeypatch.setattr(
        alignment_memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=32 * GB, available=17 * GB),
    )
    threads, memory = get_resource_defaults("6", None)
    alignment_memory.check_mapping_memory(
        ref, "bwa", threads=threads, memory=memory, samblaster=True
    )
    assert "Continuing with bwa" in caplog.text


def test_small_input_does_not_inherit_full_genome_transient_allowance(
    tmp_path, monkeypatch
):
    ref = _reference(tmp_path, bases=20_000, index=True)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 4 * GB)
    alignment_memory.check_mapping_memory(
        ref, "bwa-mem2", threads="2", memory="100M", samblaster=True, reads=300
    )


@pytest.mark.parametrize("reference_source", ["fasta", "library", "configured"])
def test_realign_refuses_mem2_memory_before_extracting(
    tmp_path, monkeypatch, reference_source
):
    from wgsextract_cli.commands import realign
    from wgsextract_cli.core.config import settings
    from wgsextract_cli.core.reference_assets import fasta_signature

    library = tmp_path / "library"
    library.mkdir()
    ref = _reference(library, bases=3_000_000_000, index=True)
    (library / "hg19.fa").write_text(">chr2\nA\n")
    (library / "hg19.fa.fai").write_text("chr2\t1\t6\t1\t2\n")
    source = tmp_path / "source.bam"
    source.write_bytes(b"input")
    monkeypatch.setitem(settings, "reference_library", str(library))
    monkeypatch.setattr(realign, "calculate_bam_md5", lambda *_a: fasta_signature(ref))
    targets = []

    def check_target(target, *args, **kwargs):
        targets.append(target)
        alignment_memory.check_mapping_memory(target, *args, **kwargs)

    monkeypatch.setattr(realign, "check_mapping_memory", check_target)
    monkeypatch.setattr(realign, "indexed_read_count", lambda *_: 679_639_896)
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: GB)
    monkeypatch.setattr(
        realign,
        "alignments_to_fastq",
        lambda *_a, **_k: pytest.fail("extraction started"),
    )
    args = Namespace(
        input=str(source),
        ref={"fasta": ref, "library": str(library), "configured": None}[
            reference_source
        ],
        r1=None,
        r2=None,
        platform="illumina",
        aligner="bwa-mem2",
        long_read=False,
        preset=None,
        threads="6",
        memory="512M",
        outdir=str(tmp_path),
        force_extract=False,
    )
    with pytest.raises(WGSExtractError, match="bwa-mem2 alignment is estimated"):
        realign.run(args)
    assert targets == [ref]


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
    tmp_path, monkeypatch, caplog
):
    from wgsextract_cli.core import warnings

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
    monkeypatch.setattr(bwa_mem2, "available_memory", lambda: low.total)
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    kwargs = {"threads": "6", "memory": "256M"}
    assert align.short_read_aligner("auto", ref, reads=count, **kwargs) == "bwa-mem2"
    assert align.short_read_aligner("auto", ref, reads=count * 2, **kwargs) == "bwa"
    asked = []
    monkeypatch.setattr(
        warnings, "check_free_space", lambda _path, size: asked.append(size)
    )
    warn_alignment_sort_space(tmp_path, count)
    warn_alignment_sort_space(tmp_path, count * 2)
    assert asked[1] > asked[0]


def test_h2_read_count_calibration_is_about_18_gib_for_bwa(tmp_path):
    ref = _reference(tmp_path, bases=3_217_346_917)
    budget = alignment_memory.estimate_mapping_memory(
        ref, "bwa", reads=679_639_896, threads="6", memory="256M"
    )
    assert 18 <= budget.total / GB < 18.5
    assert budget.samblaster >= 6_015_033_344


@pytest.mark.parametrize("reads", [800_000_000, None])
def test_bwa_setup_preserves_realign_index_count_for_selection_and_guard(
    tmp_path, monkeypatch, reads
):
    ref = _reference(tmp_path, bases=3_217_346_917)
    setup = align._AlignSetup("6", "256M", str(tmp_path), ref, "sample")
    monkeypatch.setattr(align, "_prepare_alignment", lambda *_: setup)
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(
        align, "fastq_input_reads", lambda *_: pytest.fail("resampled indexed input")
    )
    budgets = []

    def select(*_args, **kwargs):
        budgets.append(kwargs["reads"])
        return "bwa"

    monkeypatch.setattr(align, "short_read_aligner", select)
    monkeypatch.setattr(
        align, "check_mapping_memory", lambda *_a, **k: budgets.append(k["reads"])
    )
    monkeypatch.setattr(align, "ensure_bwa_index", lambda *_: None)
    monkeypatch.setattr(align, "ensure_bwa_alt_file", lambda *_: None)
    monkeypatch.setattr(
        align, "warn_alignment_sort_space", lambda _out, count: budgets.append(count)
    )
    align._bwa_setup(
        Namespace(aligner="auto", r1="r1.fq", r2="r2.fq", _input_read_count=reads),
        "alignment",
    )
    assert budgets == [reads] * 3


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
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 64 * GB)
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
