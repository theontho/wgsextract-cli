"""bam merge and bam to-ubam on tiny synthetic BAMs."""

import argparse
import subprocess
from pathlib import Path

import pytest

from wgsextract_cli.commands._bam_ubam import cmd_to_ubam, read_group_from_name
from wgsextract_cli.commands.bam import cmd_merge
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

needs_samtools = pytest.mark.skipif(
    not get_tool_path("samtools"), reason="samtools missing"
)

_SQ = "@SQ\tSN:chr1\tLN:1000\n@SQ\tSN:chr2\tLN:1000\n"
_RG = "@RG\tID:L1\tSM:S\tPL:ILLUMINA\n"
_SEQ, _QUAL = "ACGTACGTAC", "IIIIIJJJJJ"


def _samtools(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        [get_tool_path("samtools"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _bam(path: Path, records: list[str], sq: str = _SQ, rg: str = _RG) -> str:
    """Write a coordinate-sorted BAM from SAM records (``{rg}`` -> RG tag)."""
    tag = "\tRG:Z:L1" if rg else ""
    body = "".join(r.format(rg=tag) + "\n" for r in records)
    sam = "@HD\tVN:1.6\tSO:coordinate\n" + sq + rg + body
    _samtools("view", "-b", "-o", str(path), "-", stdin=sam)
    return str(path)


def _pair(name: str, chrom: str, pos: int) -> list[str]:
    mate = pos + 50
    return [
        f"{name}\t99\t{chrom}\t{pos}\t60\t10M\t=\t{mate}\t60\t{_SEQ}\t{_QUAL}"
        "\tNM:i:0\tAS:i:10{rg}",
        f"{name}\t147\t{chrom}\t{mate}\t60\t10M\t=\t{pos}\t-60\t{_SEQ}\t{_QUAL}"
        "\tNM:i:0\tAS:i:10{rg}",
    ]


@pytest.mark.parametrize(
    ("qname", "expected"),
    [
        ("A00123:8:HABCDEFXX:2:1101:1000:2000", ("HABCDEFXX.2", "ILLUMINA")),
        ("ML150009184L1C001R00100083573", ("ML150009184.1", "DNBSEQ")),
        ("E100012345L3C001R0010000001", ("E100012345.3", "DNBSEQ")),
        ("read1", (None, None)),
    ],
)
def test_read_group_from_name(qname, expected):
    assert read_group_from_name(qname) == expected


def _ubam_args(tmp_path: Path, **kw) -> argparse.Namespace:
    args = argparse.Namespace(
        input=None,
        r1=None,
        r2=None,
        sample=None,
        library="lib1",
        platform=None,
        outdir=str(tmp_path / "out"),
        ref=None,
        threads="2",
        memory="100M",
    )
    for key, value in kw.items():
        setattr(args, key, value)
    return args


@needs_samtools
def test_to_ubam_strips_alignment_and_name_sorts(tmp_path):
    reads = _pair("r2", "chr1", 100) + _pair("r1", "chr2", 100)
    reads.insert(1, reads[0].replace("\t99\t", "\t2147\t"))  # supplementary
    bam = _bam(tmp_path / "s.bam", reads)
    cmd_to_ubam(_ubam_args(tmp_path, input=bam))

    out = str(tmp_path / "out" / "s_unaligned.bam")
    header = _samtools("view", "-H", out)
    assert "SO:queryname" in header and "@SQ" not in header
    assert "@RG\tID:L1\tSM:S\tPL:ILLUMINA" in header
    records = [line.split("\t") for line in _samtools("view", out).splitlines()]
    assert [(r[0], r[1]) for r in records] == [
        ("r1", "77"),
        ("r1", "141"),
        ("r2", "77"),
        ("r2", "141"),
    ]
    assert all(r[2] == "*" and r[11:] == ["RG:Z:L1"] for r in records)
    # The reverse-strand mate is restored to its sequenced orientation.
    assert records[1][9] == "GTACGTACGT" and records[1][10] == _QUAL[::-1]


@needs_samtools
def test_to_ubam_adds_read_group_to_fastq(tmp_path):
    name = "A00123:8:HABCDEFXX:2:1101:1000:2000"
    r1, r2 = tmp_path / "x_R1.fq", tmp_path / "x_R2.fq"
    r1.write_text(f"@{name} 1:N:0:ACGT\n{_SEQ}\n+\n{_QUAL}\n")
    r2.write_text(f"@{name} 2:N:0:ACGT\n{_SEQ}\n+\n{_QUAL}\n")
    cmd_to_ubam(_ubam_args(tmp_path, r1=str(r1), r2=str(r2), sample="NA1"))

    out = str(tmp_path / "out" / "x_unaligned.bam")
    assert (
        "@RG\tID:HABCDEFXX.2\tSM:NA1\tLB:lib1\tPU:HABCDEFXX.2\tPL:ILLUMINA"
        in _samtools("view", "-H", out)
    )
    records = _samtools("view", out).splitlines()
    assert [r.split("\t")[1] for r in records] == ["77", "141"]
    assert all("BC:Z:ACGT" in r and "RG:Z:HABCDEFXX.2" in r for r in records)


def _merge_args(tmp_path: Path, inputs: list[str], **kw) -> argparse.Namespace:
    args = argparse.Namespace(
        inputs=inputs,
        output=None,
        cram=False,
        attach_rg=False,
        outdir=str(tmp_path / "out"),
        ref=None,
        threads="2",
    )
    for key, value in kw.items():
        setattr(args, key, value)
    return args


@needs_samtools
def test_merge_combines_identical_read_groups(tmp_path):
    a = _bam(tmp_path / "a.bam", _pair("r1", "chr1", 100) + _pair("r3", "chr2", 5))
    b = _bam(tmp_path / "b.bam", _pair("r2", "chr1", 120))
    cmd_merge(_merge_args(tmp_path, [a, b]))

    out = tmp_path / "out" / "merged.bam"
    assert (tmp_path / "out" / "merged.bam.bai").exists()
    assert _samtools("view", "-H", str(out)).count("@RG") == 1
    records = [line.split("\t") for line in _samtools("view", str(out)).splitlines()]
    assert [(r[0], r[2], r[3]) for r in records] == [
        ("r1", "chr1", "100"),
        ("r2", "chr1", "120"),
        ("r1", "chr1", "150"),
        ("r2", "chr1", "170"),
        ("r3", "chr2", "5"),
        ("r3", "chr2", "55"),
    ]
    assert all(r[-1] == "RG:Z:L1" for r in records)


@needs_samtools
def test_merge_rejects_different_references(tmp_path):
    a = _bam(tmp_path / "a.bam", _pair("r1", "chr1", 100))
    b = _bam(
        tmp_path / "b.bam",
        _pair("r2", "chr1", 100),
        sq="@SQ\tSN:chr1\tLN:2000\n@SQ\tSN:chr2\tLN:1000\n",
    )
    with pytest.raises(WGSExtractError, match="different sequence dictionary"):
        cmd_merge(_merge_args(tmp_path, [a, b]))
    assert not (tmp_path / "out" / "merged.bam").exists()
