"""Trio genotyping from alignments: calling, the joint filters, and routing."""

import argparse
import random
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from wgsextract_cli.commands.vcf import filter_trio
from wgsextract_cli.commands.vcf.trio_alignments import (
    is_alignment,
    joint_filter,
    joint_trio_vcf,
    split_multiallelic,
    write_isolated_denovo,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

needs_htslib = pytest.mark.skipif(
    not all(get_tool_path(t) for t in ("bcftools", "bgzip", "tabix", "samtools")),
    reason="bcftools/htslib/samtools missing",
)

# id, ALT, then proband / mother / father as GT:DP:AD. Haploid genotypes are a
# son's and his father's chrX outside the PARs.
SITES = [
    ("denovo", "G", "0/1:20:10,10", "0/0:20:20,0", "0/0:20:20,0"),
    ("parent_has_alt_read", "G", "0/1:20:10,10", "0/0:20:19,1", "0/0:20:20,0"),
    ("child_weak", "G", "0/1:20:15,5", "0/0:20:20,0", "0/0:20:20,0"),
    ("shallow_parent", "G", "0/1:20:10,10", "0/0:9:9,0", "0/0:20:20,0"),
    ("recessive", "G", "1/1:20:0,20", "0/1:20:10,10", "0/1:20:10,10"),
    ("comphet", "G", "0/1:20:10,10", "0/1:20:10,10", "0/0:20:20,0"),
    ("inherited_het", "G", "0/1:20:10,10", "1/1:20:0,20", "0/0:20:20,0"),
    ("hemizygous_denovo", "G", "1:20:0,20", "0/0:20:20,0", "0:20:20,0"),
    ("xlinked_recessive", "G", "1:20:0,20", "0/1:20:10,10", "0:20:20,0"),
    # Neither parent carries allele G, though both are het for something.
    ("wrong_allele", "G,T", "1/1:20:0,20,0", "0/2:20:10,0,10", "0/2:20:10,0,10"),
    # One alternate allele from each parent.
    ("two_alleles", "G,T", "1/2:20:0,10,10", "0/1:20:10,10,0", "0/2:20:10,0,10"),
]


def _bgzip_index(plain: Path) -> str:
    out = Path(str(plain) + ".gz")
    with open(out, "wb") as f:
        subprocess.run([get_tool_path("bgzip"), "-c", str(plain)], stdout=f, check=True)
    subprocess.run([get_tool_path("tabix"), "-p", "vcf", str(out)], check=True)
    return str(out)


def _joint_vcf(tmp_path: Path) -> str:
    plain = tmp_path / "joint.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr20,length=1000000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="d">\n'
        '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="a">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
        "PROBAND\tMOTHER\tFATHER\n"
        + "".join(
            f"chr20\t{100 * (i + 1)}\t{site}\tA\t{alt}\t99\tPASS\t.\tGT:DP:AD\t"
            f"{p}\t{m}\t{f}\n"
            for i, (site, alt, p, m, f) in enumerate(SITES)
        )
    )
    return split_multiallelic(_bgzip_index(plain), str(tmp_path / "split.vcf.gz"))


@needs_htslib
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("denovo", ["denovo", "hemizygous_denovo"]),
        ("recessive", ["recessive", "xlinked_recessive"]),
        # Split, the two-allele child is het for each allele from one parent.
        ("comphet", ["comphet", "two_alleles", "two_alleles"]),
    ],
)
def test_joint_filters_need_real_evidence(tmp_path, mode, expected):
    kept = subprocess.run(
        [get_tool_path("bcftools"), "query", "-i", joint_filter(mode, 15)]
        + ["-f", "%ID\n", _joint_vcf(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert kept == expected


def _alignment(tmp_path: Path, name: str, ref_seq: str, alt_reads: int) -> str:
    """40 reads over position 1,000 of chr20, alt_reads of them with C there.
    Every file says SM:WGSE, as WGS Extract v4 wrote."""
    sam = tmp_path / f"{name}.sam"
    lines = [
        f"@SQ\tSN:chr20\tLN:{len(ref_seq)}",
        "@RG\tID:1\tSM:WGSE\tLB:lb\tPL:ILLUMINA",
    ]
    for i in range(40):
        start = 950 + i  # 1-based; each read covers 1,000
        seq = list(ref_seq[start - 1 : start - 1 + 100])
        if i < alt_reads:
            seq[1000 - start] = "C"
        lines.append(
            f"r{i}\t0\tchr20\t{start}\t60\t100M\t*\t0\t0\t{''.join(seq)}\t{'I' * 100}"
            "\tRG:Z:1"
        )
    sam.write_text("\n".join(lines) + "\n")
    bam = tmp_path / f"{name}.bam"
    samtools = get_tool_path("samtools")
    subprocess.run([samtools, "sort", "-o", str(bam), str(sam)], check=True)
    subprocess.run([samtools, "index", str(bam)], check=True)
    return str(bam)


@needs_htslib
def test_joint_calling_names_each_file_and_finds_the_de_novo(tmp_path):
    rng = random.Random(20)
    ref_seq = "".join(rng.choice("ACGT") for _ in range(2000))
    ref_seq = ref_seq[:999] + "A" + ref_seq[1000:]  # position 1,000 is A
    ref = tmp_path / "ref.fa"
    ref.write_text(f">chr20\n{ref_seq}\n")
    subprocess.run([get_tool_path("samtools"), "faidx", str(ref)], check=True)
    alignments = [
        _alignment(tmp_path, "child", ref_seq, alt_reads=20),
        _alignment(tmp_path, "mother", ref_seq, alt_reads=0),
        _alignment(tmp_path, "father", ref_seq, alt_reads=0),
    ]
    out = joint_trio_vcf(
        alignments,
        ["M", "F", "M"],
        str(ref),
        ["--ploidy", "GRCh38"],
        str(tmp_path / "trio_joint.vcf.gz"),
        "chr20",
        1,
    )
    bcftools = get_tool_path("bcftools")
    samples = subprocess.run(
        [bcftools, "query", "-l", out], capture_output=True, text=True, check=True
    ).stdout.split()
    assert samples == ["PROBAND", "MOTHER", "FATHER"]
    calls = subprocess.run(
        [bcftools, "query", "-f", "%POS %ALT [%GT ]\n", out],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert calls == "1000 C 0/1 0/0 0/0"
    denovo = subprocess.run(
        [bcftools, "query", "-i", joint_filter("denovo", 15), "-f", "%POS\n", out],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert denovo == ["1000"]


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
    with (
        patch.object(filter_trio, "verify_dependencies"),
        pytest.raises(WGSExtractError, match="can't mix"),
    ):
        filter_trio.cmd_trio(args)


@needs_htslib
def test_isolated_denovo_drops_clusters(tmp_path):
    plain = tmp_path / "dn.vcf"
    positions = [1000, 50_000, 50_400, 50_800, 200_000, 900_000]
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr20,length=1000000>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        + "".join(f"chr20\t{p}\t.\tA\tG\t99\tPASS\t.\n" for p in positions)
    )
    out = tmp_path / "isolated.vcf.gz"
    # 50,000-50,800 is a chain of candidates 400 bp apart: all clustered.
    assert write_isolated_denovo(_bgzip_index(plain), str(out)) == (3, 6)
    kept = subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", "%POS\n", str(out)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert kept == ["1000", "200000", "900000"]


def test_a_trio_on_different_references_is_refused(tmp_path):
    files = [tmp_path / f"{n}.cram" for n in ("child", "mother", "father")]
    for f in files:
        f.write_bytes(b"")
    args = argparse.Namespace(
        proband=str(files[0]), vcf_input=None, mother=str(files[1]),
        father=str(files[2]), outdir=str(tmp_path), mode="denovo", region=None,
        ref=None,
    )  # fmt: skip
    signatures = {str(files[0]): "hg38sig", str(files[1]): "hg38sig"}
    with (
        patch.object(filter_trio, "verify_dependencies"),
        patch.object(
            filter_trio,
            "calculate_bam_md5",
            side_effect=lambda path, _: signatures.get(path, "hg19sig"),
        ),
        pytest.raises(WGSExtractError, match="different references.*father"),
    ):
        filter_trio.cmd_trio(args)
