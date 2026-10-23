"""Commands warn when they succeed with an empty result."""

import io
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from wgsextract_cli.commands import _microarray_combined as combined
from wgsextract_cli.core import output_checks
from wgsextract_cli.core.dependencies import get_tool_path

pytestmark = pytest.mark.skipif(
    not all(get_tool_path(t) for t in ("bcftools", "bgzip", "tabix", "samtools")),
    reason="bcftools/htslib/samtools missing",
)


def _vcf(path: Path, records: list[str]) -> str:
    plain = path.with_suffix("")
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=100000>\n"
        '##INFO=<ID=SCORE,Number=1,Type=Float,Description="s">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n" + "".join(records)
    )
    with open(path, "wb") as out:
        subprocess.run(
            [get_tool_path("bgzip"), "-c", str(plain)], stdout=out, check=True
        )
    subprocess.run([get_tool_path("tabix"), "-p", "vcf", str(path)], check=True)
    return str(path)


def _records(n: int, scored: set[int] = frozenset()) -> list[str]:
    return [
        f"chr1\t{i + 1}\t.\tA\tG\t50\tPASS\t{'SCORE=1.5' if i in scored else '.'}\n"
        for i in range(n)
    ]


def test_no_variants_warns_naming_the_reference(tmp_path, caplog):
    empty = _vcf(tmp_path / "empty.vcf.gz", [])
    some = _vcf(tmp_path / "some.vcf.gz", _records(3))
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_no_variants(some, "/ref/hg38.fa", "chrM")
        assert not caplog.records
        output_checks.warn_if_no_variants(empty, "/ref/hg38.fa", "chrM")
    assert "No variants were called in chrM" in caplog.text
    assert "/ref/hg38.fa" in caplog.text


def test_no_reads_warns(tmp_path, caplog):
    sam = tmp_path / "x.sam"
    sam.write_text("@SQ\tSN:chrY\tLN:1000\n")
    bam = tmp_path / "x.bam"
    subprocess.run(
        [get_tool_path("samtools"), "view", "-b", "-o", str(bam), str(sam)], check=True
    )
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_no_reads(bam, "chrY reads", "a female sample has none")
    assert "has no reads" in caplog.text and "female" in caplog.text


@pytest.mark.parametrize(
    ("n", "scored", "warns"),
    [
        (1000, set(), True),  # a whole sample's worth, none scored: wrong build
        (1000, {999}, False),  # one scored, even the last record
        (10, set(), False),  # a small VCF can miss every annotation
    ],
)
def test_nothing_annotated_warns_only_for_a_large_vcf(
    tmp_path, caplog, n, scored, warns
):
    vcf = _vcf(tmp_path / "a.vcf.gz", _records(n, scored))
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_nothing_annotated(vcf, "CHROM,POS,INFO/SCORE", "Test")
    assert ("got a Test value" in caplog.text) == warns


def test_microarray_kit_of_only_no_calls_warns(caplog):
    lines = [f"rs{i}\tchr1\t{i}\t./.\n" for i in range(1, 4)]
    out = io.StringIO()
    with (
        patch.object(combined, "_command_lines", return_value=iter(lines)),
        caplog.at_level(logging.WARNING),
    ):
        combined._write_alignment_mode_kit(out, outdir=".", out_vcf="x.vcf.gz")
    assert out.getvalue().count("\t--\n") == 3
    assert "All 3 target genotypes are no-calls" in caplog.text


def test_microarray_vcf_matching_no_targets_warns(tmp_path, caplog):
    targets = tmp_path / "targets.tab"
    targets.write_text("#CHROM\tPOS\tID\tREF\n1\t100\trs1\tA\n")
    args = type("A", (), {"region": None})()
    out = io.StringIO()
    with (
        # The input VCF's variants are all elsewhere (another build).
        patch.object(combined, "_command_lines", return_value=iter(["1\t555\tA/G\n"])),
        patch.object(combined, "_reference_alleles", return_value={}),
        patch.object(combined, "MIN_TARGETS_TO_EXPECT_A_VARIANT", 1),
        caplog.at_level(logging.WARNING),
    ):
        combined._write_vcf_mode_kit(
            out,
            args=args,
            outdir=str(tmp_path),
            out_vcf="sample.vcf.gz",
            ref_fasta="ref.fa",
            ref_vcf_tab=str(targets),
        )
    assert "rs1\t1\t100\tAA" in out.getvalue()
    assert "every genotype is written as the reference allele" in caplog.text
