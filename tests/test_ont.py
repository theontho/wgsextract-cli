import gzip
import random
import subprocess
from argparse import Namespace
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.commands import align, realign
from wgsextract_cli.commands.info.metrics import determine_sequencer
from wgsextract_cli.core import sequencing
from wgsextract_cli.core.config import settings
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


@pytest.mark.parametrize(
    ("suffix", "qname", "header", "detected"),
    [
        (".fq", "m84000_230101_120000_s1/123456789/ccs", "", "pacbio"),
        (".fq", "608478_2-3317701967", "", "ultima"),
        (".fq", "E100000001L1C001R001000001", "", "dnbseq"),
        (".bam", "renamed", "@RG\tID:r\tPL:PACBIO\n", "pacbio"),
        (".bam", "renamed", "@RG\tID:r\tPL:DNBSEQ\n", "dnbseq"),
        (".bam", "608478_2-3317701967", "", "ultima"),
        (".sam", "renamed", "@RG\tID:r\tPL:ULTIMA\n", "ultima"),
    ],
)
def test_other_platforms_keep_main_illumina_command_lines(
    tmp_path, monkeypatch, suffix, qname, header, detected
):
    reads = tmp_path / f"reads{suffix}"
    reads.write_text(f"@{qname}\nACGT\n+\nIIII\n")
    for module in (align, sequencing):
        monkeypatch.setattr(module, "get_bam_header", lambda *_args: header)
    monkeypatch.setattr(
        sequencing,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, f"{qname}\t4\t*\n", ""),
    )
    assert sequencing.input_platform(reads) == detected
    # Having pbmm2 installed must not turn reporting into an automatic switch.
    monkeypatch.setattr(
        align, "get_tool_path", lambda tool: tool if tool == "pbmm2" else None
    )
    setup = SimpleNamespace(
        threads="1", memory="256M", ref="ref.fa", outdir=str(tmp_path), stem="reads"
    )
    monkeypatch.setattr(align, "_bwa_setup", lambda *_a, **_k: (setup, "bwa"))
    commands = []
    monkeypatch.setattr(
        align,
        "run_streamed_alignment",
        lambda command, *_a, **_k: commands.append(command),
    )
    for function in ("align_pbmm2", "align_minimap2", "align_ultima"):
        monkeypatch.setattr(
            align,
            function,
            lambda *_a, **_k: pytest.fail("unexpected non-ONT platform switch"),
        )
    args = Namespace(
        r1=str(reads),
        r2=None,
        platform=None,
        aligner="auto",
        long_read=False,
        preset=None,
        gpu=False,
        format="BAM",
    )
    align.run(args)
    assert args.platform == "illumina"
    args.platform = "illumina"
    align.run(args)
    expected = [
        "bwa",
        "mem",
        "-K",
        "100000000",
        "-t",
        "1",
        "-R",
        "@RG\\tID:reads\\tSM:reads\\tLB:reads\\tPL:ILLUMINA",
        "ref.fa",
        str(reads),
    ]
    assert commands == [expected, expected]


@pytest.mark.parametrize("failure", ["exit", "exception"])
def test_failed_cram_name_probe_does_not_silently_select_paired_defaults(
    tmp_path, monkeypatch, failure
):
    cram = tmp_path / "reads.cram"
    cram.write_bytes(b"synthetic")
    monkeypatch.setattr(sequencing, "get_bam_header", lambda *_args: "")

    def probe(*_args, **_kwargs):
        if failure == "exception":
            raise OSError("probe unavailable")
        return subprocess.CompletedProcess([], 1, "", "reference unavailable")

    monkeypatch.setattr(sequencing, "run_command", probe)
    # Reporting-only callers (variant calling) keep treating this as unknown.
    assert sequencing.input_platform(cram, "original.fa") is None
    with pytest.raises(WGSExtractError, match="CRAM read names.*paired short-read"):
        sequencing.input_platform(cram, "original.fa", strict=True)
    args = Namespace(platform=None, input_ref="original.fa")
    monkeypatch.setattr(align, "get_bam_header", lambda *_args: "")
    with pytest.raises(WGSExtractError, match="CRAM read names"):
        align.resolve_platform(args, str(cram))


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


def test_realign_platform_probe_uses_input_decoding_reference(monkeypatch):
    seen = []

    def detect(reads, reference, *, strict=False):
        seen.append((reads, reference, strict))
        return "ont"

    monkeypatch.setattr(align, "input_platform", detect)
    args = Namespace(platform=None, input_ref="original.fa")
    assert align.resolve_platform(args, "reads.cram") == "ont"
    assert seen == [("reads.cram", "original.fa", True)]


@pytest.mark.parametrize("skip_preflight", [False, True])
def test_ont_realign_honors_disk_space_preflight(tmp_path, monkeypatch, skip_preflight):
    bam = tmp_path / "ont.bam"
    bam.write_bytes(b"synthetic")
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nACGT\n")
    monkeypatch.setattr(realign, "resolve_reference", lambda *_args: str(ref))
    monkeypatch.setattr(realign, "indexed_read_count", lambda *_args: 2)
    monkeypatch.setattr(realign, "check_free_space", lambda *_args: False)
    runs = []
    monkeypatch.setattr(align, "run", lambda args: runs.append(args))
    args = Namespace(
        input=str(bam),
        r1=None,
        r2=None,
        platform="ont",
        long_read=False,
        aligner="auto",
        gpu=False,
        preset=None,
        ref=str(ref),
        threads="1",
        memory="256M",
        outdir=str(tmp_path / "out"),
        keep_fastq=False,
        no_preflight=skip_preflight,
    )
    if skip_preflight:
        realign.run(args)
        assert len(runs) == 1
    else:
        with pytest.raises(WGSExtractError, match="Insufficient free disk"):
            realign.run(args)
        assert not runs


@needs_tools("samtools")
def test_bam_without_read_group_uses_read_name(tmp_path):
    bam = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:1000", f"{UUID}\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII"],
        tmp_path / "renamed.bam",
    )
    assert sequencing.input_platform(bam) == "ont"


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize(
    ("command", "source_kind"),
    [
        (command, kind)
        for command in ("align", "realign")
        for kind in ("bam", "fastq", "cram")
    ]
    + [("realign", "cram-library")],
)
def test_ont_modification_tags_survive_alignment_and_sort(
    tmp_path, monkeypatch, command, source_kind
):
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
    if source_kind == "fastq":
        path = tmp_path / "ont.fq"
        path.write_text(
            "".join(
                f"@{UUID[:-1]}{i} runid=abc ch=5\tMM:Z:C+m,0,1;"
                f"\tML:B:C,255,128\tMN:i:2000\n{seq}\n+\n{'I' * len(seq)}\n"
                for i, seq in enumerate(seqs)
            )
        )
        reads = str(path)
    elif source_kind in {"cram", "cram-library"}:
        mapped = []
        for i, record in enumerate(records):
            fields = record.split("\t")
            fields[1:6] = [
                str(16 if i else 0),
                "chr1",
                str(4001 if i else 501),
                "60",
                "2000M",
            ]
            fields[9] = genome[4000:6000] if i else genome[500:2500]
            mapped.append("\t".join(fields))
        mapped_bam = sam_to_bam(
            ["@SQ\tSN:chr1\tLN:8000", *mapped], tmp_path / "mapped.bam"
        )
        cram = tmp_path / "ont.cram"
        encoded_with = ref
        if source_kind == "cram-library":
            encoded_with = write_fasta(tmp_path / "encoding.fa", {"chr1": genome})
        run_command(
            ["samtools", "view", "-C", "-T", encoded_with, "-o", cram, mapped_bam]
        )
        run_command(["samtools", "index", cram])
        if source_kind == "cram-library":
            for path in tmp_path.glob("encoding.fa*"):
                path.unlink()
            library = tmp_path / "library"
            (library / "genomes").mkdir(parents=True)
            write_fasta(library / "genomes" / "original.fa", {"chr1": genome})
            monkeypatch.setitem(settings, "reference_library", str(library))
        reads = str(cram)
    out = tmp_path / "result"
    opts = ["--r1", reads] if command == "align" else ["--input", reads]
    if command == "realign" and source_kind == "fastq":
        opts = ["--input", bam]
        opts += ["--r1", reads]
    if command == "realign" and source_kind == "cram":
        opts += ["--input-ref", str(ref)]
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
