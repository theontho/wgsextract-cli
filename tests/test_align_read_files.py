"""align --r1 with a BAM, CRAM or SAM of the reads (#447)."""

import os
import random
import shutil
import sys

import pytest

from tests.fake_tools import python_tool
from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.cli.parser import build_parser, parse_args
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.genome_library import apply_genome_selection
from wgsextract_cli.core.utils import UsageError, run_command

TAGS = "\tMM:Z:C+m,0,1;\tML:B:C,255,128"


def _revcomp(seq: str) -> str:
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def _view(path, *opts):
    out = run_command(["samtools", "view", *opts, str(path)], capture_output=True)
    return [line.split("\t") for line in out.stdout.splitlines()]


@pytest.fixture
def genome(tmp_path):
    rng = random.Random(447)
    seq = "".join(rng.choice("ACGT") for _ in range(8000))
    return seq, write_fasta(tmp_path / "ref.fa", {"chr1": seq})


def _hifi_bam(tmp_path, seq, name="hifi.bam", read_group="@RG\tID:r\tPL:PACBIO\tSM:s"):
    reads = {"m1/1/ccs": seq[500:2500], "m1/2/ccs": _revcomp(seq[4000:6000])}
    records = [
        f"{name}\t4\t*\t0\t0\t*\t*\t0\t0\t{read}\t{'I' * len(read)}{TAGS}"
        for name, read in reads.items()
    ]
    # A secondary and a supplementary copy of the first read: each read is
    # aligned once, from its primary record.
    copy = records[0].split("\t")
    records += ["\t".join([copy[0], flag, *copy[2:]]) for flag in ("260", "2052")]
    bam = sam_to_bam([read_group, *records], tmp_path / name)
    return bam, reads


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize("platform", ["hifi", "clr", "pacbio"])
def test_minimap2_aligns_long_reads_from_a_bam(tmp_path, genome, platform):
    """#447: minimap2 was handed the BAM itself unless the reads were ONT and
    failed with 'failed to parse the FASTA/FASTQ record'."""
    seq, ref = genome
    bam, reads = _hifi_bam(tmp_path, seq)
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--r1", bam, "--platform", platform, "--aligner", "minimap2",
        "--ref", str(ref), "--outdir", str(out), "--threads", "1",
        "--memory", "256M",
    )  # fmt: skip

    primary = _view(out / "hifi_aligned.bam", "-F", "0x904")
    assert sorted(r[0] for r in primary) == sorted(reads)
    assert {(r[2], r[3]) for r in primary} == {("chr1", "501"), ("chr1", "4001")}
    assert {r[9] for r in primary} == {seq[500:2500], seq[4000:6000]}
    assert all("MM:Z:C+m,0,1;" in r[11:] for r in primary)


@pytest.mark.skipif(sys.platform == "win32", reason="pbmm2 has no Windows build")
@needs_tools("samtools", "minimap2")
def test_pbmm2_aligns_a_pacbio_bam_without_a_platform(tmp_path, genome, monkeypatch):
    """pbmm2 reads a PacBio BAM itself, so the short-read refusal must not
    stop 'align --r1 x.bam --aligner pbmm2' when --platform is left out."""
    seq, ref = genome
    bam, reads = _hifi_bam(tmp_path, seq)
    # pbmm2 is an optional PacBio tool: this one maps with the real minimap2.
    tools = {t: shutil.which(t) for t in ("samtools", "minimap2")}
    python_tool(
        tmp_path / "bin",
        "pbmm2",
        f"""import subprocess, sys
T = {tools!r}
_, ref, reads, out = sys.argv[1:5]
fq = subprocess.run([T["samtools"], "fastq", reads], capture_output=True, check=True)
sam = subprocess.run([T["minimap2"], "-ax", "map-hifi", ref, "-"], input=fq.stdout,
                     capture_output=True, check=True)
subprocess.run([T["samtools"], "sort", "-o", out, "-"], input=sam.stdout, check=True)
""",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    clear_tool_caches()
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--r1", bam, "--aligner", "pbmm2", "--ref", str(ref),
        "--outdir", str(out), "--threads", "1", "--memory", "256M",
    )  # fmt: skip

    primary = _view(out / "hifi_aligned.bam", "-F", "0x904")
    assert sorted(r[0] for r in primary) == sorted(reads)
    assert {(r[2], r[3]) for r in primary} == {("chr1", "501"), ("chr1", "4001")}


@needs_tools("samtools", "bwa")
@pytest.mark.parametrize(
    "aligner", [["--aligner", "minimap2", "--preset", "sr"], ["--aligner", "bwa"]]
)
def test_short_reads_in_a_bam_are_sent_to_realign(tmp_path, genome, aligner):
    """#447: a short-read BAM --r1 reached minimap2 or bwa as if it were FASTQ.
    align refuses it before writing anything, and the realign it names works."""
    seq, ref = genome
    records = []
    for i, start in enumerate((1000, 3000, 5000)):
        r1, r2 = seq[start : start + 100], _revcomp(seq[start + 200 : start + 300])
        records += [
            f"p{i}\t77\t*\t0\t0\t*\t*\t0\t0\t{r1}\t{'I' * 100}",
            f"p{i}\t141\t*\t0\t0\t*\t*\t0\t0\t{r2}\t{'I' * 100}",
        ]
    bam = sam_to_bam(["@RG\tID:r\tPL:ILLUMINA\tSM:s", *records], tmp_path / "s.bam")
    out = tmp_path / "out"
    common = ["--ref", str(ref), "--outdir", str(out), "--threads", "1",
              "--memory", "256M"]  # fmt: skip

    with pytest.raises(UsageError, match=r"wgsextract realign --input"):
        run_wgsextract("align", "--r1", bam, *aligner, *common)
    assert not out.exists() or not any(out.iterdir())

    run_wgsextract("realign", "--input", bam, *common, "--no-preflight")

    (aligned,) = out.glob("*_aligned.bam")
    mapped = _view(aligned, "-F", "0x90c")  # primary, mate mapped
    assert sorted((r[0], int(r[3])) for r in mapped) == [
        (f"p{i}", start + offset)
        for i, start in enumerate((1001, 3001, 5001))
        for offset in (0, 200)
    ]


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize(
    ("name", "read_type", "preset"),
    [("s.ccs.bam", "CCS", "map-hifi"), ("s.subreads.bam", "SUBREAD", "map-pb")],
)
def test_genome_workspace_of_pacbio_reads_aligns(
    tmp_path, genome, name, read_type, preset
):
    """#460 review: 'align --genome DIR' picks a PacBio *.ccs.bam or
    *.subreads.bam as --r1, but the platform resolved to illumina and align
    refused the BAM, pointing to realign."""
    seq, ref = genome
    rg = f"@RG\tID:r\tPL:PACBIO\tDS:READTYPE={read_type}\tSM:s"
    bam, reads = _hifi_bam(tmp_path, seq, name, rg)
    library = tmp_path / "genomes"
    (library / "sample").mkdir(parents=True)
    shutil.copy(bam, library / "sample" / name)
    settings["genome_library"] = str(library)
    out = tmp_path / "out"

    args, explicit = parse_args(*build_parser(), [
        "--genome", "sample", "align", "--aligner", "minimap2", "--ref", str(ref),
        "--outdir", str(out), "--threads", "1", "--memory", "256M",
    ])  # fmt: skip
    apply_genome_selection(args, explicit)
    args.func(args)

    (aligned,) = out.glob("*_aligned.bam")
    primary = _view(aligned, "-F", "0x904")
    assert sorted(r[0] for r in primary) == sorted(reads)
    assert all("MM:Z:C+m,0,1;" in r[11:] for r in primary)
    header = run_command(
        ["samtools", "view", "-H", str(aligned)], capture_output=True
    ).stdout
    # contig: subreads (CLR) map with minimap2's map-pb, HiFi with map-hifi.
    assert f"-ax {preset}" in header


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize("platform", ["hifi", "ont"])
def test_long_reads_in_fasta_keep_their_missing_qualities(tmp_path, genome, platform):
    """#460 review: FASTA long reads went through samtools import, which made
    up a quality for every base; minimap2 reading the FASTA writes QUAL '*'."""
    seq, ref = genome
    fasta = tmp_path / "reads.fa"
    fasta.write_text(f">r1\n{seq[500:2500]}\n>r2\n{_revcomp(seq[4000:6000])}\n")
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--r1", str(fasta), "--platform", platform,
        "--aligner", "minimap2", "--ref", str(ref), "--outdir", str(out),
        "--threads", "1", "--memory", "256M",
    )  # fmt: skip

    primary = _view(out / "reads_aligned.bam", "-F", "0x904")
    assert sorted((r[0], r[3]) for r in primary) == [("r1", "501"), ("r2", "4001")]
    assert {r[10] for r in primary} == {"*"}


@needs_tools("samtools", "minimap2")
def test_a_cram_of_long_reads_decodes_with_its_own_reference(
    tmp_path, genome, monkeypatch
):
    """#460 review: samtools fastq got no reference for a CRAM --r1, so
    decoding it fell back to the CRAM's UR path, then REF_PATH or the EBI
    server. align decodes it with --input-ref, or --ref when that encoded it."""
    seq, ref = genome
    ref = str(ref)
    encoding = str(write_fasta(tmp_path / "encoding.fa", {"chr1": seq}))
    # The same genome under another contig name did not encode the CRAM.
    other = str(write_fasta(tmp_path / "other.fa", {"chrA": seq}))
    records = [
        f"o1\t0\tchr1\t501\t60\t2000M\t*\t0\t0\t{seq[500:2500]}\t{'I' * 2000}{TAGS}",
    ]
    bam = sam_to_bam(["@HD\tVN:1.6\tSO:coordinate", f"@SQ\tSN:chr1\tLN:{len(seq)}",
                      "@RG\tID:r\tPL:ONT\tSM:s", *records], tmp_path / "ont.bam")  # fmt: skip
    cram = str(tmp_path / "ont.cram")
    run_command(["samtools", "view", "-C", "-T", encoding, "-o", cram, bam])
    for path in tmp_path.glob("encoding.fa*"):
        path.unlink()
    # No reference server or cache: only the FASTA align passes decodes it.
    monkeypatch.setenv("REF_PATH", str(tmp_path / "no-ref-path"))
    monkeypatch.setenv("REF_CACHE", str(tmp_path / "no-ref-cache"))
    common = ["--platform", "ont", "--aligner", "minimap2", "--threads", "1",
              "--memory", "256M"]  # fmt: skip

    with pytest.raises(UsageError, match=r"--input-ref"):
        run_wgsextract("align", "--r1", cram, "--ref", other, "--outdir",
                       str(tmp_path / "refused"), *common)  # fmt: skip
    assert not (tmp_path / "refused").exists()

    for target, opts in (("chr1", ["--ref", ref]),
                         ("chrA", ["--ref", other, "--input-ref", ref])):  # fmt: skip
        out = tmp_path / target
        run_wgsextract("align", "--r1", cram, *opts, "--outdir", str(out), *common)

        primary = _view(out / "ont_aligned.bam", "-F", "0x904")
        assert [(r[0], r[2], r[3]) for r in primary] == [("o1", target, "501")]
        assert primary[0][9] == seq[500:2500]
        assert "MM:Z:C+m,0,1;" in primary[0][11:]
