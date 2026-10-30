"""Commands warn when they succeed with an empty result."""

import io
import logging
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib
from wgsextract_cli.commands import _microarray_combined as combined
from wgsextract_cli.core import output_checks
from wgsextract_cli.core.dependencies import get_tool_path

pytestmark = needs_htslib


def _vcf(path: Path, records: list[str], tags: tuple[str, ...] = ("SCORE",)) -> str:
    plain = path.with_suffix("")
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=100000>\n"
        + "".join(
            f'##INFO=<ID={t},Number=.,Type=String,Description="t">\n' for t in tags
        )
        + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        + "".join(records)
    )
    return bgzip_index(plain)


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


def test_gatk_style_zero_index_does_not_warn_for_real_records(
    tmp_path, monkeypatch, caplog
):
    vcf = _vcf(tmp_path / "gatk.vcf.gz", _records(3))
    monkeypatch.setattr(
        output_checks,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="0\n"),
    )
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_no_variants(vcf, "/ref/hg38.fa", "chr1")
    assert not caplog.records


@pytest.mark.parametrize(
    ("line", "exit_code", "expected"),
    [("chr1\t1\t.\tA\tG\t50\tPASS\t.\n", 0, None), ("", 0, 0), ("", 1, None)],
)
def test_zero_index_probes_only_one_record(monkeypatch, line, exit_code, expected):
    monkeypatch.setattr(
        output_checks,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="0\n"),
    )
    proc = Mock()
    proc.stdout.readline.return_value = line
    proc.wait.return_value = exit_code
    start = Mock(return_value=proc)
    monkeypatch.setattr(output_checks, "popen", start)
    assert output_checks.vcf_record_count("sample.vcf.gz") == expected
    start.assert_called_once_with(
        ["bcftools", "view", "-H", "sample.vcf.gz"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    proc.stdout.readline.assert_called_once_with()
    proc.stdout.read.assert_not_called()
    proc.stdout.close.assert_called_once_with()
    proc.wait.assert_called_once_with()
    assert proc.kill.called == bool(line)


def test_positive_index_count_never_streams_records(monkeypatch):
    monkeypatch.setattr(
        output_checks,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="124581\n"),
    )
    monkeypatch.setattr(
        output_checks, "popen", lambda *_a, **_k: pytest.fail("unnecessary scan")
    )
    assert output_checks.vcf_record_count("sample.vcf.gz") == 124581


def test_zero_index_with_unreadable_vcf_does_not_warn(monkeypatch, caplog):
    monkeypatch.setattr(
        output_checks,
        "run_command",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="0\n"),
    )
    monkeypatch.setattr(output_checks, "popen", Mock(side_effect=OSError("unreadable")))
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_no_variants("sample.vcf.gz")
    assert not caplog.records


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


@pytest.mark.parametrize(
    ("columns", "tag"),
    [("CLNSIG,CLNDN", "CLNSIG"), ("INFO/GNOMAD_AF:=INFO/AF", "GNOMAD_AF")],
)
def test_nothing_annotated_reads_each_callers_column_form(
    tmp_path, caplog, columns, tag
):
    # ClinVar names bare INFO tags; gnomAD's VCF path renames AF into GNOMAD_AF.
    records = [r.replace("SCORE=1.5", f"{tag}=x") for r in _records(1000)]
    vcf = _vcf(tmp_path / "a.vcf.gz", records, tags=(tag,))
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_nothing_annotated(vcf, columns, "Test")
    assert "got a Test value" in caplog.text


def test_read_presence_uses_the_index_or_stops_at_the_first_read(tmp_path):
    samtools = get_tool_path("samtools")
    sam = tmp_path / "x.sam"
    sam.write_text(
        "@SQ\tSN:chr1\tLN:1000\nr1\t0\tchr1\t10\t60\t4M\t*\t0\t0\tACGT\tIIII\n"
    )
    full, empty = tmp_path / "full.bam", tmp_path / "empty.bam"
    subprocess.run([samtools, "view", "-b", "-o", str(full), str(sam)], check=True)
    header = tmp_path / "h.sam"
    header.write_text("@SQ\tSN:chr1\tLN:1000\n")
    subprocess.run([samtools, "view", "-b", "-o", str(empty), str(header)], check=True)
    # Unindexed: the first-read probe.
    assert output_checks.bam_has_reads(full) is True
    assert output_checks.bam_has_reads(empty) is False
    # Indexed: idxstats, where zero reads is a valid answer.
    for bam in (full, empty):
        subprocess.run([samtools, "index", str(bam)], check=True)
    assert output_checks.bam_has_reads(full) is True
    assert output_checks.bam_has_reads(empty) is False


def test_microarray_kit_with_no_rows_warns(caplog):
    out = io.StringIO()
    with (
        patch.object(combined, "_command_lines", return_value=iter([])),
        caplog.at_level(logging.WARNING),
    ):
        combined._write_alignment_mode_kit(out, outdir=".", out_vcf="x.vcf.gz")
    assert "No target genotypes were produced" in caplog.text


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
