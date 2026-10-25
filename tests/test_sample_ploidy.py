"""bcftools call applies a ploidy's M lines only to samples listed as male."""

import random
from argparse import Namespace

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, vcf_calls, write_fasta
from wgsextract_cli.commands.info import metrics
from wgsextract_cli.commands.vcf import basic
from wgsextract_cli.core import sample_ploidy


@pytest.fixture
def chry_bam(tmp_path):
    """30 reads over a chrY SNP at 1000, from sample 'man'."""
    rng = random.Random(1)
    seq = "".join(rng.choice("ACGT") for _ in range(3000))
    alt = "A" if seq[999] != "A" else "C"
    ref = write_fasta(tmp_path / "ref.fa", {"chrY": seq})
    rows = ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chrY\tLN:3000", "@RG\tID:r\tSM:man"]
    for i in range(30):
        start = 900 + i * 3
        read = seq[start - 1 : start + 149]
        read = read[: 1000 - start] + alt + read[1001 - start :]
        rows.append(f"r{i}\t0\tchrY\t{start}\t60\t150M\t*\t0\t0\t{read}\t{'I' * 150}"
                    "\tRG:Z:r")  # fmt: skip
    return str(ref), sam_to_bam(rows, tmp_path / "man.bam")


@needs_htslib
@pytest.mark.parametrize(("sex", "calls"), [("Male", 1), ("Female", 0)])
def test_snp_calls_chry_for_a_man_only(tmp_path, monkeypatch, chry_bam, sex, calls):
    ref, bam = chry_bam
    monkeypatch.setattr(metrics, "sample_sex", lambda *_a: sex)
    out = tmp_path / "out"
    basic.cmd_snp(
        Namespace(input=bam, ref=ref, outdir=str(out), threads="1", memory=None,
                  region=None, ploidy="GRCh38", ploidy_file=None)
    )  # fmt: skip
    assert len(vcf_calls(out / "snps.vcf.gz")) == calls
    assert not list(out.glob("*.samples.txt"))


@needs_htslib
def test_samples_are_named_as_mpileup_names_them(tmp_path, chry_bam):
    _ref, bam = chry_bam
    assert sample_ploidy.alignment_samples(bam) == ["man"]
    rows = ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chrY\tLN:3000"]
    no_rg = sam_to_bam(rows, tmp_path / "plain.bam")
    assert sample_ploidy.alignment_samples(no_rg) == [no_rg]
