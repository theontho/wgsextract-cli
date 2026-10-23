"""Trio genotyping from alignments: the joint filters, and routing by input type."""

import argparse
import subprocess
from pathlib import Path

import pytest

from wgsextract_cli.commands.vcf import filter_trio
from wgsextract_cli.commands.vcf.trio_alignments import is_alignment, joint_filter
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

needs_bcftools = pytest.mark.skipif(
    not all(get_tool_path(t) for t in ("bcftools", "bgzip", "tabix")),
    reason="bcftools/htslib missing",
)

# id, then proband / mother / father as GT:DP:AD.
SITES = [
    ("denovo", "0/1:20:10,10", "0/0:20:20,0", "0/0:20:20,0"),
    ("parent_has_alt_read", "0/1:20:10,10", "0/0:20:19,1", "0/0:20:20,0"),
    ("child_weak", "0/1:20:15,5", "0/0:20:20,0", "0/0:20:20,0"),
    ("shallow_parent", "0/1:20:10,10", "0/0:9:9,0", "0/0:20:20,0"),
    ("recessive", "1/1:20:0,20", "0/1:20:10,10", "0/1:20:10,10"),
    ("comphet", "0/1:20:10,10", "0/1:20:10,10", "0/0:20:20,0"),
    ("inherited_het", "0/1:20:10,10", "1/1:20:0,20", "0/0:20:20,0"),
]


def _joint_vcf(path: Path) -> str:
    plain = path.with_suffix("")
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr20,length=1000000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="d">\n'
        '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="a">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
        "PROBAND\tMOTHER\tFATHER\n"
        + "".join(
            f"chr20\t{100 * (i + 1)}\t{site}\tA\tG\t99\tPASS\t.\tGT:DP:AD\t{p}\t{m}\t{f}\n"
            for i, (site, p, m, f) in enumerate(SITES)
        )
    )
    with open(path, "wb") as out:
        subprocess.run(
            [get_tool_path("bgzip"), "-c", str(plain)], stdout=out, check=True
        )
    subprocess.run([get_tool_path("tabix"), "-p", "vcf", str(path)], check=True)
    return str(path)


@needs_bcftools
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("denovo", ["denovo"]), ("recessive", ["recessive"]), ("comphet", ["comphet"])],
)
def test_joint_filters_need_real_evidence(tmp_path, mode, expected):
    vcf = _joint_vcf(tmp_path / "joint.vcf.gz")
    kept = subprocess.run(
        [get_tool_path("bcftools"), "query", "-i", joint_filter(mode, 15)]
        + ["-f", "%ID\n", vcf],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert kept == expected


def test_alignments_are_recognised():
    assert is_alignment("x.cram") and is_alignment("X.BAM")
    assert not is_alignment("x.vcf.gz")


def test_a_trio_mixing_vcfs_and_alignments_is_refused(tmp_path):
    files = {name: tmp_path / name for name in ("p.cram", "m.vcf.gz", "f.cram")}
    for f in files.values():
        f.write_bytes(b"")
    args = argparse.Namespace(
        proband=str(files["p.cram"]), vcf_input=None, mother=str(files["m.vcf.gz"]),
        father=str(files["f.cram"]), outdir=str(tmp_path), mode="denovo", region=None,
    )  # fmt: skip
    with pytest.raises(WGSExtractError, match="can't mix"):
        filter_trio.cmd_trio(args)
