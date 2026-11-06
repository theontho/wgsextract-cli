"""align --r1 with a BAM, CRAM or SAM of the reads (#447)."""

import random

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
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


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize("platform", ["hifi", "clr", "pacbio"])
def test_minimap2_aligns_long_reads_from_a_bam(tmp_path, genome, platform):
    """#447: minimap2 was handed the BAM itself unless the reads were ONT and
    failed with 'failed to parse the FASTA/FASTQ record'."""
    seq, ref = genome
    reads = {"m1/1/ccs": seq[500:2500], "m1/2/ccs": _revcomp(seq[4000:6000])}
    records = [
        f"{name}\t4\t*\t0\t0\t*\t*\t0\t0\t{read}\t{'I' * len(read)}{TAGS}"
        for name, read in reads.items()
    ]
    # A secondary and a supplementary copy of the first read: each read is
    # aligned once, from its primary record.
    copy = records[0].split("\t")
    records += ["\t".join([copy[0], flag, *copy[2:]]) for flag in ("260", "2052")]
    bam = sam_to_bam(["@RG\tID:r\tPL:PACBIO\tSM:s", *records], tmp_path / "hifi.bam")
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
