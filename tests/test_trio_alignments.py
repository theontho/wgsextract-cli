"""Trio genotyping from alignments: calling, the joint filters, and routing."""

import argparse
import random
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
)
from wgsextract_cli.commands.vcf import filter_trio
from wgsextract_cli.commands.vcf.trio_alignments import (
    is_alignment,
    joint_filter,
    joint_trio_vcf,
    split_multiallelic,
    write_isolated_denovo,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

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
    return split_multiallelic(bgzip_index(plain), str(tmp_path / "split.vcf.gz"))


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
    return sam_to_bam(lines, tmp_path / f"{name}.bam")


@needs_htslib
@pytest.mark.parametrize("folder", ["plain", "with space"])
def test_joint_calling_names_each_file_and_finds_the_de_novo(tmp_path, folder):
    tmp_path = tmp_path / folder
    tmp_path.mkdir()
    rng = random.Random(20)
    ref_seq = "".join(rng.choice("ACGT") for _ in range(2000))
    ref_seq = ref_seq[:999] + "A" + ref_seq[1000:]  # position 1,000 is A
    ref = write_fasta(tmp_path / "ref.fa", {"chr20": ref_seq})
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
    assert write_isolated_denovo(bgzip_index(plain), str(out)) == (3, 6)
    kept = subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", "%POS\n", str(out)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert kept == ["1000", "200000", "900000"]


def _header_bam(path: Path, contigs: dict[str, int]) -> str:
    sam = path.with_suffix(".sam")
    sam.write_text("".join(f"@SQ\tSN:{n}\tLN:{ln}\n" for n, ln in contigs.items()))
    subprocess.run(
        [get_tool_path("samtools"), "view", "-b", "-o", str(path), str(sam)],
        check=True,
    )
    return str(path)


@needs_htslib
def test_trio_references_are_compared_on_the_called_chromosomes(tmp_path):
    child = _header_bam(tmp_path / "child.bam", {"chr20": 2000, "chrUn_decoy": 50})
    # Only an extra decoy differs: compatible.
    mother = _header_bam(tmp_path / "mother.bam", {"chr20": 2000})
    # Another build: chr20's length differs.
    father = _header_bam(tmp_path / "father.bam", {"chr20": 3000})
    args = argparse.Namespace(
        proband=child, vcf_input=None, mother=mother, father=mother,
        outdir=str(tmp_path), mode="denovo", region="chr20", ref=None,
    )  # fmt: skip
    with (
        patch.object(filter_trio, "verify_dependencies"),
        patch.object(filter_trio, "calculate_bam_md5", return_value=None),
        # Past the check, the run then needs a reference.
        pytest.raises(WGSExtractError, match="Reference genome required"),
    ):
        filter_trio.cmd_trio(args)
    args.father = father
    with (
        patch.object(filter_trio, "verify_dependencies"),
        pytest.raises(WGSExtractError, match="father's alignment father.bam isn't"),
    ):
        filter_trio.cmd_trio(args)


@needs_htslib
def test_trio_contigs_follow_every_region_listed(tmp_path):
    from wgsextract_cli.commands.vcf.trio_alignments import trio_contig_lengths

    bam = _header_bam(tmp_path / "x.bam", {"chr1": 100, "chr2": 200, "chr3": 300})
    assert trio_contig_lengths(bam, "chr1,chr2") == {"chr1": 100, "chr2": 200}
    assert trio_contig_lengths(bam, "chr1:1-50,chr3:10-20") == {
        "chr1": 100,
        "chr3": 300,
    }
    assert list(trio_contig_lengths(bam)) == ["chr1", "chr2", "chr3"]


def test_chromosomes_are_split_into_regions(monkeypatch):
    from wgsextract_cli.commands.vcf import trio_alignments

    monkeypatch.setattr(trio_alignments, "CHUNK_BP", 100)
    assert trio_alignments.trio_regions({"chr1": 250, "chr2": 100}) == [
        "chr1:1-100",
        "chr1:101-200",
        "chr1:201-250",
        "chr2:1-100",
    ]


@needs_htslib
def test_a_proband_without_variants_has_nothing_to_genotype(tmp_path):
    rng = random.Random(20)
    ref_seq = "".join(rng.choice("ACGT") for _ in range(2000))
    ref = write_fasta(tmp_path / "ref.fa", {"chr20": ref_seq})
    alignments = [
        _alignment(tmp_path, name, ref_seq, alt_reads=0)
        for name in ("child", "mother", "father")
    ]
    with pytest.raises(WGSExtractError, match="no variants in chr20"):
        joint_trio_vcf(
            alignments,
            ["M", "F", "M"],
            str(ref),
            ["--ploidy", "GRCh38"],
            str(tmp_path / "trio_joint.vcf.gz"),
            "chr20",
            1,
        )


def _sexed_trio(tmp_path: Path, proband_y_reads: int) -> tuple[str, list[str]]:
    """A reference of chr20, chrX and chrY and the trio's BAMs on it: the child
    has a de novo C at chr20:1,000; everyone has 20 chrX reads, and the child
    proband_y_reads chrY reads, which tell its sex."""
    rng = random.Random(7)
    chr20 = "".join(rng.choice("ACGT") for _ in range(2000))
    chr20 = chr20[:999] + "A" + chr20[1000:]
    sex = {n: "".join(rng.choice("ACGT") for _ in range(2000)) for n in "XY"}
    ref = write_fasta(
        tmp_path / "ref.fa", {"chr20": chr20, "chrX": sex["X"], "chrY": sex["Y"]}
    )
    bams = []
    for name in ("child", "mother", "father"):
        bam = _alignment(tmp_path, name, chr20, alt_reads=20 if name == "child" else 0)
        lines = subprocess.run(
            [get_tool_path("samtools"), "view", "-h", bam],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        header = [line for line in lines if line.startswith("@")]
        header[1:1] = ["@SQ\tSN:chrX\tLN:2000", "@SQ\tSN:chrY\tLN:2000"]
        reads = [line for line in lines if not line.startswith("@")]
        y_reads = proband_y_reads if name == "child" else 0
        for contig, count in (("X", 20), ("Y", y_reads)):
            reads += [
                f"{contig}{i}\t0\tchr{contig}\t{101 + i}\t60\t100M\t*\t0\t0\t"
                f"{sex[contig][100 + i : 200 + i]}\t{'I' * 100}\tRG:Z:1"
                for i in range(count)
            ]
        bams.append(sam_to_bam(header + reads, Path(bam)))
    return str(ref), bams


class _Hg38Library:
    """The reference library of an hg38 FASTA without a ploidy file."""

    build = "hg38"
    ploidy_file: str | None = None

    def __init__(self, ref, *_args, **_kwargs):
        self.fasta = ref


def _positions(vcf: Path) -> list[str]:
    return subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", "%POS\n", str(vcf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


@needs_htslib
@pytest.mark.parametrize(
    ("input_option", "cram"),
    [("--proband", False), ("--input", False), ("--input", True)],
    ids=["proband-bam", "input-bam", "input-cram"],
)
def test_trio_from_alignments_infers_sex_and_isolates_the_de_novo(
    tmp_path, input_option, cram
):
    ref, (child, mother, father) = _sexed_trio(tmp_path, proband_y_reads=20)
    if cram:
        converted = []
        for bam in (child, mother, father):
            output = str(Path(bam).with_suffix(".cram"))
            run_command(["samtools", "view", "-C", "-T", ref, "-o", output, bam])
            run_command(["samtools", "index", output])
            converted.append(output)
        child, mother, father = converted
    out = tmp_path / "out"
    with (
        patch.object(filter_trio, "ReferenceLibrary", _Hg38Library),
        patch.object(
            filter_trio, "joint_trio_vcf", wraps=filter_trio.joint_trio_vcf
        ) as joint,
    ):
        run_wgsextract(
            "vcf", "trio", input_option, child, "--mother", mother, "--father",
            father, "--ref", ref, "--region", "chr20", "--mode", "all",
            "--threads", "1", "--outdir", str(out),
        )  # fmt: skip

    sexes, _ref, ploidy = joint.call_args.args[1:4]
    assert sexes == ["M", "F", "M"]
    assert ploidy == ["--ploidy", "GRCh38"]
    assert _positions(out / "trio_denovo.vcf.gz") == ["1000"]
    assert _positions(out / "trio_denovo_isolated.vcf.gz") == ["1000"]
    assert _positions(out / "trio_recessive.vcf.gz") == []
    assert _positions(out / "trio_comphet.vcf.gz") == []
    assert not [p.name for p in out.iterdir() if p.name.startswith(".wgse")]


@needs_htslib
def test_trio_from_alignments_needs_a_sex_and_a_ploidy(tmp_path):
    # chrX reads but no chrY ones: a girl.
    ref, (child, mother, father) = _sexed_trio(tmp_path, proband_y_reads=0)
    out = tmp_path / "out"
    argv = [
        "vcf", "trio", "--proband", child, "--mother", mother, "--father", father,
        "--ref", ref, "--region", "chr20", "--threads", "1", "--outdir", str(out),
    ]  # fmt: skip
    # The bare FASTA's build is unknown, so its chrX ploidy is too.
    with pytest.raises(WGSExtractError, match="No ploidy for reference build"):
        run_wgsextract(*argv)

    ploidy_txt = tmp_path / "ploidy.txt"
    ploidy_txt.write_text("chrX 1 2000 M 1\n* * * M 2\n* * * F 2\n")

    class WithPloidyFile(_Hg38Library):
        ploidy_file = str(ploidy_txt)

    with (
        patch.object(filter_trio, "ReferenceLibrary", WithPloidyFile),
        patch.object(
            filter_trio, "joint_trio_vcf", wraps=filter_trio.joint_trio_vcf
        ) as joint,
    ):
        run_wgsextract(*argv)
    assert joint.call_args.args[1] == ["F", "F", "M"]
    assert joint.call_args.args[3] == ["--ploidy-file", str(ploidy_txt)]
    assert _positions(out / "trio_denovo.vcf.gz") == ["1000"]

    unknown_sex = patch(
        "wgsextract_cli.commands.info.metrics.sample_sex", return_value="Unknown"
    )
    with (
        patch.object(filter_trio, "ReferenceLibrary", _Hg38Library),
        unknown_sex,
        pytest.raises(WGSExtractError, match="pass --proband-sex"),
    ):
        run_wgsextract(*argv)
    with (
        patch.object(filter_trio, "ReferenceLibrary", _Hg38Library),
        unknown_sex,
        patch.object(
            filter_trio, "joint_trio_vcf", wraps=filter_trio.joint_trio_vcf
        ) as joint,
    ):
        run_wgsextract(*argv, "--proband-sex", "male")
    assert joint.call_args.args[1] == ["M", "F", "M"]


@needs_htslib
def test_trio_from_alignments_without_candidates_writes_empty_outputs(tmp_path):
    ref, (child, mother, father) = _sexed_trio(tmp_path, proband_y_reads=20)
    out = tmp_path / "out"
    with patch.object(filter_trio, "ReferenceLibrary", _Hg38Library):
        # 40 reads each can't meet a 50-read minimum.
        run_wgsextract(
            "vcf", "trio", "--proband", child, "--mother", mother, "--father",
            father, "--ref", ref, "--region", "chr20", "--threads", "1",
            "--min-depth", "50", "--outdir", str(out),
        )  # fmt: skip
    assert _positions(out / "trio_denovo.vcf.gz") == []
    assert _positions(out / "trio_denovo_isolated.vcf.gz") == []
