"""Commands warn when they succeed with an empty result."""

import gzip
import logging
import struct
import subprocess
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib
from wgsextract_cli.core import output_checks
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import run_command

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


def _htsjdk_index(vcf: str) -> str:
    """Re-index ``vcf`` like htsjdk (GATK), whose .tbi has no record counts:
    htslib's index less its per-contig pseudo-bins, so --nrecords reports 0."""
    run_command([get_tool_path("tabix"), "-f", "-p", "vcf", vcf])
    data = gzip.decompress(Path(f"{vcf}.tbi").read_bytes())
    n_ref, *_, l_nm = struct.unpack_from("<8i", data, 4)
    pos = 36 + l_nm
    out = bytearray(data[:pos])
    for _ in range(n_ref):
        (n_bin,) = struct.unpack_from("<i", data, pos)
        pos += 4
        bins = []
        for _ in range(n_bin):
            bin_id, n_chunk = struct.unpack_from("<Ii", data, pos)
            end = pos + 8 + 16 * n_chunk
            if bin_id != 37450:  # the pseudo-bin holding the counts
                bins.append(data[pos:end])
            pos = end
        out += struct.pack("<i", len(bins)) + b"".join(bins)
        (n_intv,) = struct.unpack_from("<i", data, pos)
        out += data[pos : pos + 4 + 8 * n_intv]
        pos += 4 + 8 * n_intv
    Path(f"{vcf}.tbi").write_bytes(gzip.compress(bytes(out + data[pos:])))
    return vcf


def test_gatk_style_zero_index_does_not_warn_for_real_records(tmp_path, caplog):
    vcf = _htsjdk_index(_vcf(tmp_path / "gatk.vcf.gz", _records(3)))
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_no_variants(vcf, "/ref/hg38.fa", "chr1")
    assert not caplog.records


def test_record_count_comes_from_the_index_or_the_records(tmp_path):
    counted = _vcf(tmp_path / "counted.vcf.gz", _records(3))
    assert output_checks.vcf_record_count(counted) == 3
    # A zero from an index without counts is confirmed from the records.
    uncounted = _htsjdk_index(_vcf(tmp_path / "uncounted.vcf.gz", _records(3)))
    assert output_checks.vcf_record_count(uncounted) is None
    empty = _htsjdk_index(_vcf(tmp_path / "empty.vcf.gz", []))
    assert output_checks.vcf_record_count(empty) == 0
    Path(empty).write_bytes(b"not a VCF")
    assert output_checks.vcf_record_count(empty) is None


def test_unreadable_vcf_does_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        output_checks.warn_if_no_variants(tmp_path / "gone.vcf.gz")
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
