import gzip
import os
import random
import re
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from tests.disk_volumes import GIB, Volumes
from tests.fake_tools import python_tool
from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.commands import align
from wgsextract_cli.commands.info.metrics import determine_sequencer
from wgsextract_cli.core import alignment_memory, results, sequencing
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import clear_tool_caches
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


@pytest.fixture
def genome_ref(tmp_path):
    genome = "".join(random.Random(3).choices("ACGT", k=3000))
    return genome, write_fasta(tmp_path / "ref.fa", {"chr1": genome})


def _onto(genome: str, qname: str) -> str:
    """An unmapped SAM record of a 100 bp read from genome[1000:1100]."""
    return f"{qname}\t4\t*\t0\t0\t*\t*\t0\t0\t{genome[1000:1100]}\t{'I' * 100}"


@needs_tools("bwa", "samtools")
@pytest.mark.parametrize(
    ("suffix", "qname", "header", "detected"),
    [
        (".fq", "m84000_230101_120000_s1/123456789/ccs", "", "pacbio"),
        (".fq", "608478_2-3317701967", "", "ultima"),
        (".fq", "E100000001L1C001R001000001", "", "dnbseq"),
        (".bam", "renamed", "@RG\tID:r\tPL:PACBIO", "pacbio"),
        (".bam", "renamed", "@RG\tID:r\tPL:DNBSEQ", "dnbseq"),
        (".bam", "608478_2-3317701967", "", "ultima"),
        (".sam", "renamed", "@RG\tID:r\tPL:ULTIMA", "ultima"),
    ],
)
def test_other_platforms_keep_illumina_alignment_defaults(
    tmp_path, monkeypatch, genome_ref, suffix, qname, header, detected
):
    """Only ONT (and an Ultima read group on a BAM/CRAM) changes the defaults;
    other detected platforms are reported, and align as before, with BWA."""
    genome, ref = genome_ref
    reads = tmp_path / f"reads{suffix}"
    mate = genome[1200:1300].translate(str.maketrans("ACGT", "TGCA"))[::-1]
    if suffix == ".fq":
        reads.write_text(f"@{qname}\n{genome[1000:1100]}\n+\n{'I' * 100}\n")
        expected = [["chr1", "1001"]]
    else:
        lines = [*([header] if header else []), _onto(genome, qname)]
        if suffix == ".bam":
            first = lines[-1].split("\t")
            first[1] = "77"
            second = [qname, "141", *first[2:9], mate, "I" * 100]
            sam_to_bam([*lines[:-1], "\t".join(first), "\t".join(second)], reads)
            expected = [["chr1", "1001"], ["chr1", "1201"]]
        else:
            reads.write_text("".join(f"{line}\n" for line in lines))
    assert sequencing.input_platform(reads) == detected
    assert align.resolve_platform(Namespace(platform=None), str(reads)) == "illumina"
    if suffix == ".sam":
        return  # align --r1 takes FASTQ and realign --input a BAM or CRAM
    # Having pbmm2 installed must not turn reporting into an automatic switch.
    bindir = tmp_path / "bin"
    python_tool(bindir, "pbmm2", "import sys; sys.exit('pbmm2 ran')")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    clear_tool_caches()

    run_wgsextract(
        *(["align", "--r1"] if suffix == ".fq" else ["realign", "--input"]),
        str(reads), "--ref", str(ref), "--outdir", str(tmp_path / "out"),
        "--threads", "1", "--memory", "256M",
    )  # fmt: skip

    aligned = tmp_path / "out" / "reads_aligned.bam"
    head = run_command(["samtools", "view", "-H", aligned], capture_output=True)
    groups = [line for line in head.stdout.splitlines() if line.startswith("@RG")]
    assert len(groups) == 1 and "\tPL:ILLUMINA" in groups[0]
    assert "\tID:bwa\t" in head.stdout
    body = run_command(["samtools", "view", aligned], capture_output=True).stdout
    assert [line.split("\t")[2:4] for line in body.splitlines()] == expected


@needs_tools("samtools")
def test_undecodable_cram_never_falls_back_to_short_read_defaults(
    tmp_path, monkeypatch, genome_ref
):
    """A CRAM whose read groups don't name the platform needs its decoding
    FASTA to read the first name; without it, ONT can't be ruled out."""
    genome, _ = genome_ref
    monkeypatch.setenv("REF_PATH", str(tmp_path / "no-ref-cache"))
    monkeypatch.setenv("REF_CACHE", str(tmp_path / "no-ref-cache"))
    (tmp_path / "enc").mkdir()
    encoded_with = write_fasta(tmp_path / "enc" / "original.fa", {"chr1": genome})
    mapped = _onto(genome, UUID).split("\t")
    mapped[1:6] = ["0", "chr1", "1001", "60", "100M"]
    bam = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:3000", "\t".join(mapped)], tmp_path / "mapped.bam"
    )
    cram = tmp_path / "reads.cram"
    run_command(["samtools", "view", "-C", "-T", encoded_with, "-o", cram, bam])
    moved = tmp_path / "moved"
    shutil.move(encoded_with.parent, moved)
    missing = str(encoded_with)

    # Reporting-only callers (variant calling) keep treating this as unknown.
    assert sequencing.input_platform(cram, missing) is None
    with pytest.raises(WGSExtractError, match="CRAM read names.*paired short-read"):
        align.resolve_platform(Namespace(platform=None, input_ref=missing), str(cram))
    # realign's --input-ref decodes it.
    args = Namespace(platform=None, input_ref=str(moved / "original.fa"))
    assert align.resolve_platform(args, str(cram)) == "ont"


@needs_tools("samtools")
def test_platform_follows_an_input_replaced_in_place(tmp_path):
    def write(platform: str) -> None:
        sam_to_bam(
            [
                f"@RG\tID:r\tPL:{platform}",
                "renamed\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII",
            ],
            tmp_path / "reads.bam",
        )

    write("ONT")
    bam = tmp_path / "reads.bam"
    assert sequencing.input_platform(bam) == "ont"
    assert sequencing.input_platform(bam) == "ont"
    write("ILLUMINA")
    assert sequencing.input_platform(bam) == "illumina"


@needs_tools("samtools", "minimap2")
@pytest.mark.parametrize("skip_preflight", [False, True])
def test_ont_realign_honors_disk_space_preflight(
    tmp_path, monkeypatch, genome_ref, skip_preflight
):
    genome, ref = genome_ref
    seq = genome[500:2500]
    bam = sam_to_bam(
        [
            "@RG\tID:r\tPL:ONT\tSM:s",
            f"{UUID}\t4\t*\t0\t0\t*\t*\t0\t0\t{seq}\t{'I' * len(seq)}",
        ],
        tmp_path / "ont.bam",
    )
    out = tmp_path / "out"
    out.mkdir()
    volumes = Volumes(monkeypatch)
    volumes.free[out] = 1
    volumes.sizes[Path(bam).absolute()] = 4 * GIB
    argv = ["realign", "--input", bam, "--ref", str(ref), "--outdir", str(out),
            "--threads", "1", "--memory", "256M"]  # fmt: skip

    if skip_preflight:
        run_wgsextract(*argv, "--no-preflight")
        assert (out / "ont_aligned.bam").is_file()
    else:
        with pytest.raises(
            WGSExtractError,
            match=f"Insufficient free disk space at {re.escape(str(out))}",
        ):
            run_wgsextract(*argv)
        assert not (out / "ont_aligned.bam").exists()


@needs_tools("samtools", "minimap2")
def test_ont_realign_warns_before_minimap2_outgrows_memory(
    tmp_path, monkeypatch, genome_ref
):
    """#428: minimap2 realignments were OOM-killed with no warning; now the
    preflight names settings that fit, and the run continues."""
    genome, ref = genome_ref
    seq = genome[500:2500]
    bam = sam_to_bam(
        [f"{UUID}\t4\t*\t0\t0\t*\t*\t0\t0\t{seq}\t{'I' * len(seq)}"],
        tmp_path / "ont.bam",
    )
    # Even a tiny input needs minimap2's thread buffers and the pipeline.
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 3 * GIB // 4)
    out = tmp_path / "out"
    result = results.CommandResult("realign")
    with results.collecting(result):
        run_wgsextract(
            "realign", "--input", bam, "--ref", str(ref), "--outdir", str(out),
            "--threads", "2", "--memory", "1G",
        )  # fmt: skip
    [warning] = [w for w in result.warnings if w.code == "alignment_memory"]
    assert warning.details["aligner"] == "minimap2"
    assert warning.details["estimated_gib"] > warning.details["available_gib"]
    assert (out / "ont_aligned.bam").is_file()


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
    + [
        ("realign", kind)
        for kind in (
            "cram-library",
            "cram-target",
            "cram-target-unindexed",
            "cram-masked-target",
        )
    ],
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
    elif source_kind.startswith("cram"):
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
        if source_kind in {"cram-library", "cram-masked-target"}:
            encoded_with = write_fasta(tmp_path / "encoding.fa", {"chr1": genome})
        run_command(
            ["samtools", "view", "-C", "-T", encoded_with, "-o", cram, mapped_bam]
        )
        run_command(["samtools", "index", cram])
        if source_kind in {"cram-library", "cram-masked-target"}:
            for path in tmp_path.glob("encoding.fa*"):
                path.unlink()
            library = tmp_path / "library"
            (library / "genomes").mkdir(parents=True)
            write_fasta(library / "genomes" / "original.fa", {"chr1": genome})
            monkeypatch.setitem(settings, "reference_library", str(library))
        else:
            # #299: re-mapping to the encoding build needs no --input-ref.
            (tmp_path / "empty-library").mkdir()
            monkeypatch.setitem(
                settings, "reference_library", str(tmp_path / "empty-library")
            )
        if source_kind == "cram-target-unindexed":
            Path(f"{ref}.fai").unlink()
        if source_kind == "cram-masked-target":
            # Same names and lengths as the encoding FASTA, different bases:
            # the CRAM must still decode with the library copy.
            ref = write_fasta(
                tmp_path / "masked.fa",
                {"chr1": genome[:1000] + "N" * 200 + genome[1200:]},
            )
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
    assert {record[9] for record in primary} == {genome[500:2500], genome[4000:6000]}
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
