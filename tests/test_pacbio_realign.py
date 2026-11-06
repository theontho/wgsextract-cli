"""#388: realign single-end PacBio BAM/CRAM through minimap2."""

import gzip
import os
import random

import pytest

from tests.fake_tools import python_tool
from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.utils import UsageError, WGSExtractError, run_command
from wgsextract_cli.main import main

COMP = str.maketrans("ACGT", "TGCA")
MODS = "MM:Z:C+m,0,1;\tML:B:C,255,128"


def _revcomp(seq: str) -> str:
    return seq.translate(COMP)[::-1]


@pytest.fixture
def genome_ref(tmp_path):
    genome = "".join(random.Random(388).choices("ACGT", k=8000))
    return genome, write_fasta(tmp_path / "ref.fa", {"chr1": genome})


def _pacbio_bam(tmp_path, genome, *, kind="aligned", read_type="CCS", paired=False):
    """Three HiFi reads with MM/ML tags. Aligned input also has a secondary and
    a supplementary record of m/1/ccs and an unmapped read; ubam is basecaller
    output (all unmapped, unsorted, no @SQ)."""
    reads = {
        "m/1/ccs": genome[500:2500],
        "m/2/ccs": _revcomp(genome[4000:6000]),
        "m/3/ccs": genome[6500:7500],
    }
    group = f"@RG\tID:hifi\tSM:HG002\tPL:PACBIO\tDS:READTYPE={read_type}"
    flag = 0x41 if paired else 0
    if kind == "ubam":
        records = [
            f"{name}\t4\t*\t0\t0\t*\t*\t0\t0\t{seq}\t{'I' * len(seq)}"
            f"\tRG:Z:hifi\t{MODS}"
            for name, seq in reads.items()
        ]
        return sam_to_bam([group, *records], tmp_path / "hifi.bam"), reads
    one, two, three = reads.values()
    records = [
        f"m/1/ccs\t{flag}\tchr1\t501\t60\t2000M\t*\t0\t0\t{one}\t{'I' * 2000}"
        f"\tRG:Z:hifi\t{MODS}",
        f"m/1/ccs\t{flag | 0x100}\tchr1\t3001\t0\t2000M\t*\t0\t0\t*\t*\tRG:Z:hifi",
        f"m/1/ccs\t{flag | 0x800}\tchr1\t4001\t9\t1000H1000M\t*\t0\t0"
        f"\t{genome[4000:5000]}\t{'I' * 1000}\tRG:Z:hifi",
        f"m/2/ccs\t16\tchr1\t4001\t60\t2000M\t*\t0\t0"
        f"\t{genome[4000:6000]}\t{'I' * 2000}\tRG:Z:hifi\t{MODS}",
        f"m/3/ccs\t4\t*\t0\t0\t*\t*\t0\t0\t{three}\t{'I' * 1000}\tRG:Z:hifi\t{MODS}",
    ]
    bam = sam_to_bam(
        ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chr1\tLN:8000", group, *records],
        tmp_path / "hifi.bam",
    )
    if kind == "cram":
        cram = tmp_path / "hifi.cram"
        ref = tmp_path / "ref.fa"
        run_command(["samtools", "view", "-C", "-T", ref, "-o", cram, bam])
        run_command(["samtools", "index", cram])
        os.unlink(bam)
        return str(cram), reads
    run_command(["samtools", "index", bam])
    return bam, reads


def _realign(source, ref, out, *extra):
    run_wgsextract(
        "realign", "--input", str(source), "--ref", str(ref), "--outdir", str(out),
        "--threads", "1", "--memory", "256M", "--no-preflight", *extra,
    )  # fmt: skip


def _records(bam):
    sam = run_command(["samtools", "view", bam], capture_output=True).stdout
    return [line.split("\t") for line in sam.splitlines()]


def _header(bam):
    return run_command(["samtools", "view", "-H", bam], capture_output=True).stdout


@needs_tools("samtools", "minimap2", "bgzip")
@pytest.mark.parametrize("kind", ["aligned", "ubam", "cram"])
@pytest.mark.parametrize("keep_fastq", [False, True], ids=["stream", "fastq"])
def test_single_end_pacbio_realigns_each_read_once_with_its_tags(
    tmp_path, genome_ref, kind, keep_fastq
):
    """#388: realign refused single-end PacBio reads ('bam unalign --se' then
    'align'). Now each read is realigned once, never again from its secondary
    or supplementary records, and keeps its MM/ML tags."""
    genome, ref = genome_ref
    source, reads = _pacbio_bam(tmp_path, genome, kind=kind)
    out = tmp_path / "out"
    _realign(source, ref, out, *(["--keep-fastq"] if keep_fastq else []))

    aligned = out / "hifi_aligned.bam"
    primary = [r for r in _records(aligned) if not int(r[1]) & 0x900]
    assert sorted(r[0] for r in primary) == sorted(reads)
    by_name = {r[0]: r for r in primary}
    for name, seq in reads.items():
        record = by_name[name]
        stored = _revcomp(record[9]) if int(record[1]) & 16 else record[9]
        assert stored == seq
        assert "MM:Z:C+m,0,1;" in record[11:] and "ML:B:C,255,128" in record[11:]
    assert by_name["m/1/ccs"][2:4] == ["chr1", "501"]
    assert by_name["m/2/ccs"][2:4] == ["chr1", "4001"]
    assert by_name["m/3/ccs"][2:4] == ["chr1", "6501"]
    header = _header(aligned)
    assert "\tPL:PACBIO" in header
    # contract: HiFi reads map with minimap2's HiFi preset, keeping tags (-y).
    assert "-ax map-hifi" in header and " -y " in header
    assert (out / "hifi_aligned.bam.bai").is_file()

    fastq = out / "hifi.fastq.gz"
    assert fastq.is_file() == keep_fastq
    if keep_fastq:
        with gzip.open(fastq, "rt") as handle:
            names = [line.split()[0] for line in handle if line.startswith("@m/")]
        assert sorted(names) == sorted(f"@{name}" for name in reads)


@needs_tools("samtools", "minimap2")
def test_pacbio_subreads_map_with_the_clr_preset(tmp_path, genome_ref):
    genome, ref = genome_ref
    source, _ = _pacbio_bam(tmp_path, genome, read_type="SUBREAD")
    _realign(source, ref, tmp_path / "out")
    # contract: CLR subreads need minimap2's map-pb, not the HiFi preset.
    assert "-ax map-pb" in _header(tmp_path / "out" / "hifi_aligned.bam")


@needs_tools("samtools", "minimap2")
def test_pacbio_realign_stays_on_minimap2_when_pbmm2_is_installed(
    tmp_path, genome_ref, monkeypatch
):
    genome, ref = genome_ref
    source, _ = _pacbio_bam(tmp_path, genome)
    bindir = tmp_path / "bin"
    python_tool(bindir, "pbmm2", "import sys; sys.exit('pbmm2 ran')")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    clear_tool_caches()
    _realign(source, ref, tmp_path / "out")
    assert "\tID:minimap2\t" in _header(tmp_path / "out" / "hifi_aligned.bam")
    with pytest.raises(UsageError, match="align --r1 <BAM> --aligner pbmm2"):
        _realign(source, ref, tmp_path / "out2", "--aligner", "pbmm2")


@needs_tools("samtools", "minimap2", "bgzip")
def test_pacbio_movie_named_input_keeps_its_fastq_and_resumes(tmp_path, genome_ref):
    """PR #457 review: movie.ccs.bam with --keep-fastq made minimap2 write
    movie.ccs_aligned.bam while realign expected movie_aligned.bam and failed;
    a later --dry-run then wrote a FASTQ manifest."""
    genome, ref = genome_ref
    source, reads = _pacbio_bam(tmp_path, genome)
    movie = tmp_path / "movie.ccs.bam"
    os.rename(source, movie)
    os.rename(f"{source}.bai", f"{movie}.bai")
    out = tmp_path / "out"
    _realign(movie, ref, out, "--keep-fastq")
    primary = [r for r in _records(out / "movie_aligned.bam") if not int(r[1]) & 0x900]
    assert sorted(r[0] for r in primary) == sorted(reads)
    assert (out / "movie.fastq.gz").is_file()
    assert not list(out.glob("movie.ccs*"))

    before = sorted(p.name for p in out.iterdir())
    main(
        ["realign", "--input", str(movie), "--ref", str(ref), "--outdir", str(out),
         "--keep-fastq", "--dry-run", "--config", str(tmp_path / "config.toml")]
    )  # fmt: skip
    assert sorted(p.name for p in out.iterdir()) == before


@needs_tools("samtools")
def test_partly_paired_pacbio_input_is_still_refused(tmp_path, genome_ref):
    """Only input whose sampled reads are all single-end takes the long-read
    path; a mix would lose its pairing there, and short-read extraction would
    drop the single-end reads."""
    genome, ref = genome_ref
    source, _ = _pacbio_bam(tmp_path, genome, paired=True)
    records = _records(source)
    assert any(int(r[1]) & 0xC0 == 0 for r in records if not int(r[1]) & 0x900)
    with pytest.raises(WGSExtractError, match="--platform for ONT, PacBio"):
        _realign(source, ref, tmp_path / "out")


@needs_tools("samtools", "minimap2", "bgzip")
def test_ont_realign_can_keep_its_tagged_fastq(tmp_path, genome_ref):
    """ONT realign refused --keep-fastq; the retained FASTQ now carries MM/ML."""
    genome, ref = genome_ref
    uuid = "a18d071b-8bb9-42fe-b929-1f24cfe1d052"
    seq = genome[500:2500]
    source = sam_to_bam(
        [
            "@RG\tID:r\tPL:ONT\tSM:HG002",
            f"{uuid}\t4\t*\t0\t0\t*\t*\t0\t0\t{seq}\t{'I' * 2000}\tRG:Z:r\t{MODS}",
        ],
        tmp_path / "ont.bam",
    )
    _realign(source, ref, tmp_path / "out", "--keep-fastq")
    with gzip.open(tmp_path / "out" / "ont.fastq.gz", "rt") as handle:
        assert "MM:Z:C+m,0,1;" in handle.readline()
    [record] = _records(tmp_path / "out" / "ont_aligned.bam")
    assert record[2:4] == ["chr1", "501"] and "MM:Z:C+m,0,1;" in record[11:]
    with pytest.raises(UsageError, match="pbmm2 aligns PacBio reads only"):
        _realign(source, ref, tmp_path / "out2", "--aligner", "pbmm2")


@needs_tools("samtools", "minimap2")
def test_align_maps_a_pacbio_unaligned_bam_with_minimap2(tmp_path, genome_ref):
    """minimap2 can't read BAM; align --r1 <HiFi uBAM> used to hand it one."""
    genome, ref = genome_ref
    source, reads = _pacbio_bam(tmp_path, genome, kind="ubam")
    run_wgsextract(
        "align", "--r1", source, "--ref", str(ref), "--platform", "hifi",
        "--aligner", "minimap2", "--outdir", str(tmp_path / "out"),
        "--threads", "1", "--memory", "256M",
    )  # fmt: skip
    primary = [
        r
        for r in _records(tmp_path / "out" / "hifi_aligned.bam")
        if not int(r[1]) & 0x900
    ]
    assert sorted(r[0] for r in primary) == sorted(reads)
    assert all("ML:B:C,255,128" in r[11:] for r in primary)


@needs_tools("samtools", "minimap2", "bgzip")
@pytest.mark.parametrize("first", [[], ["--keep-fastq"]], ids=["streamed", "kept"])
def test_finished_pacbio_realign_is_not_redone_by_a_keep_fastq_rerun(
    tmp_path, genome_ref, first
):
    """PR #457 review: rerunning a finished long-read realign with --keep-fastq
    extracted the reads again and realigned, replacing a valid BAM (hours on a
    genome)."""
    genome, ref = genome_ref
    source, _ = _pacbio_bam(tmp_path, genome)
    out = tmp_path / "out"
    _realign(source, ref, out, *first)
    aligned = out / "hifi_aligned.bam"
    finished = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}

    _realign(source, ref, out, "--keep-fastq")
    assert {p.name: p.stat().st_mtime_ns for p in out.iterdir()} == finished
    assert aligned.stat().st_mtime_ns == finished[aligned.name]

    # --force-extract asks for the FASTQ, and so a new alignment from it.
    _realign(source, ref, out, "--keep-fastq", "--force-extract")
    assert (out / "hifi.fastq.gz").is_file()
    assert aligned.stat().st_mtime_ns != finished[aligned.name]
