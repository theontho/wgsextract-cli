"""bcftools call applies a ploidy's M lines only to samples listed as male."""

import random
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, vcf_calls, write_fasta
from wgsextract_cli.commands.info import metrics
from wgsextract_cli.commands.vcf import basic
from wgsextract_cli.core import sample_ploidy


def _snp_bam(tmp_path, contig):
    """30 reads over a SNP at 1000, from sample 'man'."""
    rng = random.Random(1)
    seq = "".join(rng.choice("ACGT") for _ in range(3000))
    alt = "A" if seq[999] != "A" else "C"
    ref = write_fasta(tmp_path / "ref.fa", {contig: seq})
    rows = [
        "@HD\tVN:1.6\tSO:coordinate",
        f"@SQ\tSN:{contig}\tLN:3000",
        "@RG\tID:r\tSM:man",
    ]
    for i in range(30):
        start = 900 + i * 3
        read = seq[start - 1 : start + 149]
        read = read[: 1000 - start] + alt + read[1001 - start :]
        rows.append(f"r{i}\t0\t{contig}\t{start}\t60\t150M\t*\t0\t0\t{read}\t{'I' * 150}"
                    "\tRG:Z:r")  # fmt: skip
    return str(ref), sam_to_bam(rows, tmp_path / "man.bam")


@pytest.fixture
def chry_bam(tmp_path):
    return _snp_bam(tmp_path, "chrY")


@needs_htslib
@pytest.mark.parametrize(("sex", "calls"), [("Male", 1), ("Female", 0)])
@pytest.mark.parametrize("region", [None, "chrY"])
def test_snp_calls_chry_for_a_man_only(
    tmp_path, monkeypatch, chry_bam, sex, calls, region
):
    ref, bam = chry_bam
    monkeypatch.setattr(metrics, "sample_sex", lambda *_a: sex)
    out = tmp_path / "out"
    basic.cmd_snp(
        Namespace(input=bam, ref=ref, outdir=str(out), threads="1", memory=None,
                  region=region, ploidy="GRCh38", ploidy_file=None)
    )  # fmt: skip
    assert len(vcf_calls(out / "snps.vcf.gz")) == calls
    assert not list(out.glob("*.samples.txt"))


@pytest.mark.parametrize(
    "region", ["chr1", "20:10-100", "chr20,chr22:1-100", "chrM", "MT:1-100"]
)
@pytest.mark.parametrize("preset", ["GRCh37", "GRCh38"])
def test_autosomal_regions_do_not_scan_the_whole_alignment(
    tmp_path, monkeypatch, region, preset
):
    def no_scan(*_args):
        raise AssertionError("scanned the whole alignment")

    monkeypatch.setattr(metrics, "sample_sex", no_scan)
    with sample_ploidy.sex_samples_args(
        "man.cram", str(tmp_path), ["--ploidy", preset], region=region
    ) as args:
        assert args == []
    assert not list(tmp_path.glob("*.samples.txt"))


@pytest.mark.parametrize(
    "region",
    [None, "X", "chrY:1-100", "chr1,chrX", "{chr20:extra}:1-100", "chrUn_KI270442v1"],
)
def test_sex_chromosomes_and_unknown_region_syntax_keep_sex_detection(
    tmp_path, monkeypatch, region
):
    calls = []
    monkeypatch.setattr(
        metrics, "sample_sex", lambda *args: calls.append(args) or "Male"
    )
    monkeypatch.setattr(sample_ploidy, "alignment_samples", lambda _path: ["man"])
    with sample_ploidy.sex_samples_args(
        "man.cram", str(tmp_path), ["--ploidy", "GRCh38"], region=region
    ) as args:
        assert args[0] == "--samples-file"
        assert Path(args[1]).read_text() == "man\tM\n"
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.samples.txt"))


@pytest.mark.parametrize(
    "male_contig,needed", [("chrX", False), ("chr20", True), ("*", True)]
)
def test_custom_ploidy_rules_for_the_requested_contigs_are_respected(
    tmp_path, male_contig, needed
):
    ploidy = tmp_path / "custom.ploidy"
    ploidy.write_text(
        f"# CHROM FROM TO SEX PLOIDY\n{male_contig} 1 100 M 1\n* * * * 2\n"
    )
    assert (
        sample_ploidy._sex_aware(["--ploidy-file", str(ploidy)], "chr20:1-100")
        is needed
    )
    assert sample_ploidy._sex_aware(["--ploidy-file", str(ploidy)])


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
    ploidy = tmp_path / "human.ploidy"
    ploidy.write_text("chrX 1 100 M 1\n* * * M 2\n* * * F 2\n" + extra)
    assert (
        sample_ploidy._sex_aware(["--ploidy-file", str(ploidy)], "chr20:1-100")
        is needed
    )
    assert sample_ploidy._sex_aware(["--ploidy-file", str(ploidy)], "chrX")


@needs_htslib
def test_real_autosomal_snp_call_stays_diploid_without_a_sex_scan(
    tmp_path, monkeypatch
):
    ref, bam = _snp_bam(tmp_path, "chr20")

    def no_scan(*_args):
        raise AssertionError("scanned the whole alignment")

    monkeypatch.setattr(metrics, "sample_sex", no_scan)
    out = tmp_path / "out"
    basic.cmd_snp(
        Namespace(
            input=bam,
            ref=ref,
            outdir=str(out),
            threads="1",
            memory=None,
            region="chr20:900-1100",
            ploidy="GRCh38",
            ploidy_file=None,
        )
    )
    assert vcf_calls(out / "snps.vcf.gz")[0][-1] == "1/1"


@needs_htslib
def test_samples_are_named_as_mpileup_names_them(tmp_path, chry_bam):
    _ref, bam = chry_bam
    assert sample_ploidy.alignment_samples(bam) == ["man"]
    rows = ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chrY\tLN:3000"]
    no_rg = sam_to_bam(rows, tmp_path / "plain.bam")
    assert sample_ploidy.alignment_samples(no_rg) == [no_rg]
