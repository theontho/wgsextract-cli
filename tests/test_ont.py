import gzip
import random
from argparse import Namespace

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.commands import align
from wgsextract_cli.commands.info.metrics import determine_sequencer
from wgsextract_cli.core import sequencing
from wgsextract_cli.core.utils import WGSExtractError, run_command

UUID = "a18d071b-8bb9-42fe-b929-1f24cfe1d052"


@pytest.mark.parametrize(
    ("header", "qname", "expected"),
    [
        ("@RG\tID:r\tPL:ONT\n", "renamed", "ont"),
        ("", UUID, "ont"),
        ("@RG\tID:r\tPL:ILLUMINA\n", UUID, "illumina"),
        ("@RG\tID:r\tPL:PACBIO\n", "", "pacbio"),
        ("@RG\tID:r\tPL:ULTIMA\n", "", "ultima"),
        ("@RG\tID:a\tPL:ONT\n@RG\tID:b\tPL:ILLUMINA\n", UUID, None),
        ("", "renamed", None),
    ],
)
def test_platform_metadata(header, qname, expected):
    assert sequencing.platform_from_metadata(header, qname) == expected


def test_info_recognizes_ont_read_groups_without_uuid():
    assert determine_sequencer("renamed", "@RG\tID:r\tPL:ONT\n") == "Oxford Nanopore"
    assert sequencing.platform_from_metadata(sequencer="Oxford Nanopore") == "ont"


@pytest.mark.parametrize("compressed", [False, True])
def test_fastq_detection_and_cache_invalidation(tmp_path, compressed):
    path = tmp_path / ("reads.fq.gz" if compressed else "reads.fq")
    opener = gzip.open if compressed else open
    with opener(path, "wt") as handle:
        handle.write(f"@{UUID} runid=abc\nACGT\n+\nIIII\n")
    assert sequencing.input_platform(path) == "ont"
    args = Namespace(platform=None, long_read=False, aligner="auto", preset=None)
    args.platform = align.resolve_platform(args, str(path))
    assert align.select_aligner(args) == "minimap2"
    assert align._minimap2_preset(args) == "map-ont"
    assert (
        align.resolve_platform(Namespace(platform="illumina"), str(path)) == "illumina"
    )
    with opener(path, "wt") as handle:
        handle.write("@renamed\nACGT\n+\nIIII\n")
    assert sequencing.input_platform(path) is None


def test_ont_gpu_refuses_to_drop_modification_tags():
    with pytest.raises(WGSExtractError, match="MM/ML"):
        align.select_aligner(
            Namespace(platform="ont", long_read=False, aligner="auto", gpu=True)
        )


def test_platform_probe_is_cached_per_input_revision(tmp_path, monkeypatch):
    bam = tmp_path / "reads.bam"
    bam.write_bytes(b"synthetic")
    headers = []

    def header(*_args):
        headers.append(True)
        return "@RG\tID:r\tPL:ONT\n"

    monkeypatch.setattr(sequencing, "get_bam_header", header)
    assert sequencing.input_platform(bam) == "ont"
    assert sequencing.input_platform(bam) == "ont"
    assert len(headers) == 1
    bam.write_bytes(b"replaced")
    assert sequencing.input_platform(bam) == "ont"
    assert len(headers) == 2


@needs_tools("samtools")
def test_bam_without_read_group_uses_read_name(tmp_path):
    bam = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:1000", f"{UUID}\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII"],
        tmp_path / "renamed.bam",
    )
    assert sequencing.input_platform(bam) == "ont"


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize("command", ["align", "realign"])
@pytest.mark.parametrize("fastq", [False, True])
def test_ont_modification_tags_survive_alignment_and_sort(tmp_path, command, fastq):
    rng = random.Random(24)
    genome = "".join(rng.choices("ACGT", k=8000))
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": genome})
    seqs = [
        genome[500:2500],
        genome[4000:6000].translate(str.maketrans("ACGT", "TGCA"))[::-1],
    ]
    records = [
        f"{UUID[:-1]}{i}\t4\t*\t0\t0\t*\t*\t0\t0\t{seq}\t{'I' * len(seq)}"
        "\tMM:Z:C+m,0,1;\tML:B:C,255,128\tMN:i:2000"
        for i, seq in enumerate(seqs)
    ]
    bam = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:8000", "@RG\tID:r\tPL:ONT\tSM:HG002", *records],
        tmp_path / "ont.bam",
    )
    assert sequencing.input_platform(bam) == "ont"
    reads = bam
    if fastq:
        path = tmp_path / "ont.fq"
        path.write_text(
            "".join(
                f"@{UUID[:-1]}{i} runid=abc ch=5\tMM:Z:C+m,0,1;"
                f"\tML:B:C,255,128\tMN:i:2000\n{seq}\n+\n{'I' * len(seq)}\n"
                for i, seq in enumerate(seqs)
            )
        )
        reads = str(path)
    out = tmp_path / "result"
    opts = ["--r1", reads] if command == "align" else ["--input", bam]
    if command == "realign" and fastq:
        opts += ["--r1", reads]
    run_wgsextract(
        command,
        *opts,
        "--ref",
        str(ref),
        "--outdir",
        str(out),
        "--threads",
        "1",
        "--memory",
        "256M",
        *(["--no-preflight"] if command == "realign" else []),
    )
    result = out / "ont_aligned.bam"
    sam = run_command(["samtools", "view", result], capture_output=True).stdout
    primary = [
        line.split("\t")
        for line in sam.splitlines()
        if not int(line.split("\t")[1]) & 0x900
    ]
    assert len(primary) == 2
    assert {int(record[1]) & 16 for record in primary} == {0, 16}
    for record in primary:
        assert "MM:Z:C+m,0,1;" in record[11:]
        assert "ML:B:C,255,128" in record[11:]
        assert "MN:i:2000" in record[11:]
    recovered = run_command(
        ["samtools", "fastq", "-T", "MM,ML,MN", result], capture_output=True
    ).stdout
    original = run_command(
        ["samtools", "fastq", "-T", "MM,ML,MN", bam], capture_output=True
    ).stdout
    assert recovered == original
    assert result.with_suffix(".bam.bai").exists()
