"""bcftools call applies a ploidy's M lines only to samples listed as male."""

import random
from pathlib import Path

import pytest

from tests.htslib_helpers import (
    needs_htslib,
    run_wgsextract,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from tests.test_microarray_vendor_formats import _templates
from wgsextract_cli.core import sample_ploidy
from wgsextract_cli.core.sample_workspace import SampleWorkspace
from wgsextract_cli.core.utils import run_command

pytestmark = needs_htslib


def _snp_bam(folder: Path, *contigs: str) -> tuple[str, str]:
    """30 reads over a SNP at 1000 of each contig, from sample 'man', and
    nothing else: its idxstats read as a man's (chrY reads, no chrX) when one
    contig is chrY, else as unknown. The BAM's folder is a sample workspace,
    so a sex scan is recorded there."""
    folder.mkdir(parents=True, exist_ok=True)
    rng = random.Random(1)
    seq = "".join(rng.choice("ACGT") for _ in range(3000))
    alt = "A" if seq[999] != "A" else "C"
    ref = write_fasta(folder / "ref.fa", dict.fromkeys(contigs, seq))
    rows = ["@HD\tVN:1.6\tSO:coordinate"]
    rows += [f"@SQ\tSN:{contig}\tLN:3000" for contig in contigs]
    rows.append("@RG\tID:r\tSM:man")
    for contig in contigs:
        for i in range(30):
            start = 900 + i * 3
            read = seq[start - 1 : start + 149]
            read = read[: 1000 - start] + alt + read[1001 - start :]
            rows.append(f"{contig}r{i}\t0\t{contig}\t{start}\t60\t150M\t*\t0\t0"
                        f"\t{read}\t{'I' * 150}\tRG:Z:r")  # fmt: skip
    bam = sam_to_bam(rows, folder / "man.bam")
    run_wgsextract("sample", "set", "--input", bam, "--platform", "illumina")
    return str(ref), bam


def _scanned_sex(bam: str) -> str | None:
    """The sex a scan of the reads recorded in the BAM's sample workspace."""
    fact = SampleWorkspace(Path(bam).parent).detected("sex", bam)
    return fact.value if fact else None


def _snp(ref: str, bam: str, out: Path, *options: str) -> list[tuple[str, ...]]:
    run_wgsextract("vcf", "snp", "--input", bam, "--ref", ref,
                   "--outdir", str(out), "--threads", "1", *options)  # fmt: skip
    assert not list(out.glob("*.samples.txt"))
    return vcf_calls(out / "snps.vcf.gz")


@pytest.mark.parametrize(("sex", "calls"), [(None, 1), ("Female", 0)])
@pytest.mark.parametrize("region", [None, "chrY"])
def test_snp_calls_chry_for_a_man_only(tmp_path, sex, calls, region):
    ref, bam = _snp_bam(tmp_path / "sample", "chrY")
    if sex:
        run_wgsextract("sample", "set", "--input", bam, "--sex", sex)
    region_args = ["--region", region] if region else []
    out = tmp_path / "out"
    assert len(_snp(ref, bam, out, "--ploidy", "GRCh38", *region_args)) == calls
    assert _scanned_sex(bam) == "Male"


@pytest.mark.parametrize(
    "region", ["chr1", "20:10-100", "chr20,chr22:1-100", "chrM", "MT:1-100"]
)
@pytest.mark.parametrize("preset", ["GRCh37", "GRCh38"])
def test_autosomal_regions_do_not_scan_the_whole_alignment(tmp_path, region, preset):
    _ref, bam = _snp_bam(tmp_path / "sample", "chrY")
    with sample_ploidy.sex_samples_args(
        bam, str(tmp_path), ["--ploidy", preset], region=region
    ) as args:
        assert args == []
    assert _scanned_sex(bam) is None


@pytest.mark.parametrize(
    "region",
    [None, "X", "chrY:1-100", "chr1,chrX", "{chr20:extra}:1-100", "chrUn_KI270442v1"],
)
def test_sex_chromosomes_and_unknown_region_syntax_keep_sex_detection(tmp_path, region):
    _ref, bam = _snp_bam(tmp_path / "sample", "chrY")
    with sample_ploidy.sex_samples_args(
        bam, str(tmp_path), ["--ploidy", "GRCh38"], region=region
    ) as args:
        assert args[0] == "--samples-file"
        assert Path(args[1]).read_text() == "man\tM\n"
    assert _scanned_sex(bam) == "Male"
    assert not list(tmp_path.glob("*.samples.txt"))


def _male_samples_file(tmp_path: Path, ploidy: str, region: str | None) -> bool:
    """Whether a man's chrY BAM is listed as male for this ploidy file."""
    _ref, bam = _snp_bam(tmp_path / "sample", "chrY")
    path = tmp_path / "custom.ploidy"
    path.write_text(ploidy)
    with sample_ploidy.sex_samples_args(
        bam, str(tmp_path), ["--ploidy-file", str(path)], region=region
    ) as args:
        return bool(args)


@pytest.mark.parametrize(
    "male_contig,needed", [("chrX", False), ("chr20", True), ("*", True)]
)
def test_custom_ploidy_rules_for_the_requested_contigs_are_respected(
    tmp_path, male_contig, needed
):
    ploidy = f"# CHROM FROM TO SEX PLOIDY\n{male_contig} 1 100 M 1\n* * * * 2\n"
    assert _male_samples_file(tmp_path / "region", ploidy, "chr20:1-100") is needed
    assert _male_samples_file(tmp_path / "whole", ploidy, None)


@pytest.mark.parametrize(
    "extra,needed",
    [
        ("", False),
        ("chr20 1 100 F 1\n", True),
        ("chr20 1 100 M 2\n", True),
        ("chr20 1 100 M 1\nchr20 1 100 F 1\n", True),
    ],
)
def test_paired_diploid_fallback_can_skip_only_unambiguous_regions(
    tmp_path, extra, needed
):
    ploidy = "chrX 1 100 M 1\n* * * M 2\n* * * F 2\n" + extra
    assert _male_samples_file(tmp_path / "region", ploidy, "chr20:1-100") is needed
    assert _male_samples_file(tmp_path / "chrx", ploidy, "chrX")


def test_real_autosomal_snp_call_stays_diploid_without_a_sex_scan(tmp_path):
    ref, bam = _snp_bam(tmp_path / "sample", "chr20")
    out = tmp_path / "out"
    calls = _snp(ref, bam, out, "--region", "chr20:900-1100", "--ploidy", "GRCh38")
    assert calls[0][-1] == "1/1"
    assert _scanned_sex(bam) is None


@pytest.mark.parametrize(
    "contig,region", [("20", "chr20:900-1100"), ("chr20", "20:900-1100")]
)
def test_microarray_normalizes_region_before_custom_sex_ploidy(
    tmp_path, contig, region
):
    ref, bam = _snp_bam(tmp_path / "sample", contig, "chrY")
    _templates(tmp_path / "sample", "23andMe_V5_2025.txt", "", "rs1000\t20\t1000\n")
    targets = tmp_path / "targets.tab"
    targets.write_text(f"{contig}\t1000\trs1000\tA\n")
    run_command(["bgzip", "-f", str(targets)])
    target_gz = str(targets) + ".gz"
    run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", target_gz])
    ploidy = tmp_path / "custom.ploidy"
    ploidy.write_text(f"{contig} 1 3000 M 1\n* * * * 2\n")
    out = tmp_path / "out"
    run_wgsextract("microarray", "--input", bam, "--ref", ref,
                   "--ref-vcf-tab", target_gz, "--ploidy-file", str(ploidy),
                   "--region", region, "--formats", "23andme_v5_2025",
                   "--threads", "1", "--outdir", str(out))  # fmt: skip
    assert _scanned_sex(bam) == "Male"
    assert vcf_calls(out / "man_combined.vcf.gz")[0][-1] == "1"
    assert not list(out.glob("*.samples.txt"))
    export = next(out.glob("*23andMe_V5_2025.txt")).read_text()
    assert export == "rs1000\t20\t1000\tAA\n"


def test_samples_are_named_as_mpileup_names_them(tmp_path):
    _ref, bam = _snp_bam(tmp_path / "sample", "chrY")
    assert sample_ploidy.alignment_samples(bam) == ["man"]
    rows = ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chrY\tLN:3000"]
    no_rg = sam_to_bam(rows, tmp_path / "plain.bam")
    assert sample_ploidy.alignment_samples(no_rg) == [no_rg]
