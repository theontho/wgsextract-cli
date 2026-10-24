"""`vcf snp`, `vcf indel` and `vcf annotate` with bcftools on tiny data, and the
helpers that pick their inputs, ploidy and reference."""

import argparse
import gzip
import logging
import random
from pathlib import Path

import pytest

from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    run_wgsextract,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli.commands.vcf import basic
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError, run_command

# A het SNP (A>G) at 500 and a het 2 bp deletion (CGT>C) at 700, each carried by
# half of 24 reads; the deletion's flanks differ, so it can't be left-shifted.
SNP_POS, DEL_POS = 500, 700


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    root = tmp_path_factory.mktemp("calls")
    rng = random.Random(11)
    seq = list("".join(rng.choice("ACGT") for _ in range(1000)))
    seq[SNP_POS - 1] = "A"
    seq[DEL_POS - 2 : DEL_POS + 4] = "ACGTCA"  # 699..704
    ref_seq = "".join(seq)
    ref = write_fasta(root / "ref.fa", {"chr1": ref_seq})

    def read(start, alt):
        """(CIGAR, bases) of a 100 bp read at ``start``, carrying its variant if alt."""
        s, cigar, bases = start - 1, "100M", ref_seq[start - 1 : start + 99]
        if alt and start < SNP_POS:
            k = SNP_POS - start
            bases = bases[:k] + "G" + bases[k + 1 :]
        elif alt:
            left = DEL_POS - start + 1  # bases up to and including POS
            cigar = f"{left}M2D{100 - left}M"
            bases = ref_seq[s:DEL_POS] + ref_seq[DEL_POS + 2 : s + 102]
        return cigar, bases

    lines = ["@SQ\tSN:chr1\tLN:1000"]
    for group, first in (("snp", 440), ("del", 650)):
        for i in range(24):
            start = first + 2 * i
            cigar, bases = read(start, alt=i % 2 == 0)
            lines.append(
                f"{group}{i}\t0\tchr1\t{start}\t60\t{cigar}\t*\t0\t0\t"
                f"{bases}\t{'I' * len(bases)}"
            )
    bam = sam_to_bam(lines, root / "sample.bam")
    return {"ref": str(ref), "bam": bam}


def _call(cmd, sample, outdir, *extra):
    run_wgsextract(
        "vcf", cmd, "--input", sample["bam"], "--ref", sample["ref"],
        "--outdir", str(outdir), "--ploidy", "GRCh38", *extra,
    )  # fmt: skip


@needs_htslib
@pytest.mark.parametrize(
    ("cmd", "out", "expected"),
    [
        ("snp", "snps.vcf.gz", ("chr1", str(SNP_POS), "A", "G", "0/1")),
        ("indel", "indels.vcf.gz", ("chr1", str(DEL_POS), "CGT", "C", "0/1")),
    ],
)
def test_caller_finds_only_its_variant_type(sample, tmp_path, cmd, out, expected):
    _call(cmd, sample, tmp_path)
    assert vcf_calls(tmp_path / out) == [expected]
    assert (tmp_path / f"{out}.tbi").is_file()


@needs_htslib
@pytest.mark.parametrize("cmd", ["snp", "indel"])
def test_caller_warns_when_its_region_has_no_variants(sample, tmp_path, caplog, cmd):
    with caplog.at_level(logging.WARNING):
        _call(cmd, sample, tmp_path, "-r", "chr1:1-100")
    assert vcf_calls(tmp_path / f"{cmd}s.vcf.gz") == []
    assert "No variants were called in chr1:1-100" in caplog.text


@needs_htslib
def test_caller_applies_the_ploidy_file(sample, tmp_path):
    ploidy = tmp_path / "ploidy.txt"
    ploidy.write_text("chr1 1 1000 M 1\nchr1 1 1000 F 1\n")
    run_wgsextract(
        "vcf", "snp", "--input", sample["bam"], "--ref", sample["ref"],
        "--outdir", str(tmp_path), "--ploidy-file", str(ploidy),
    )  # fmt: skip
    # Diploid, the site is a 0/1 het; haploid, half the reads carrying G is no call.
    assert vcf_calls(tmp_path / "snps.vcf.gz") == []


def test_caller_requires_input(tmp_path):
    with pytest.raises(WGSExtractError, match="--input is required"):
        basic.get_base_args(argparse.Namespace(input=None))


@needs_htslib
def test_caller_requires_a_reference(sample, tmp_path):
    with pytest.raises(WGSExtractError, match="--ref is required for variant calling"):
        run_wgsextract(
            "vcf", "snp", "--input", sample["bam"], "--outdir", str(tmp_path)
        )
    assert list(tmp_path.iterdir()) == []


def test_missing_ploidy_file_is_reported(tmp_path):
    args = argparse.Namespace(ploidy=None, ploidy_file=str(tmp_path / "nope.txt"))
    with pytest.raises(WGSExtractError, match="--ploidy-file"):
        basic._ploidy_args(args, argparse.Namespace(ploidy_file=None, build=None))  # type: ignore[arg-type]


def test_explicit_ploidy_wins_over_the_librarys_file():
    args = argparse.Namespace(ploidy="1", ploidy_file=None)
    lib = argparse.Namespace(ploidy_file="/lib/ploidy.txt", build="hg38")
    assert basic._ploidy_args(args, lib) == ["--ploidy", "1"]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("vcf_input", "input_path", "explicit", "expected"),
    [
        ("given.vcf.gz", "in.vcf.gz", set(), "given.vcf.gz"),
        ("default.vcf.gz", "in.vcf.gz", {"input"}, "in.vcf.gz"),
        ("default.vcf.gz", "in.vcf.gz", set(), "default.vcf.gz"),
        (None, "in.vcf.gz", set(), "in.vcf.gz"),
        (None, None, set(), None),
    ],
)
def test_vcf_input_selection(monkeypatch, vcf_input, input_path, explicit, expected):
    monkeypatch.setitem(settings, "default_input_vcf", "default.vcf.gz")
    args = argparse.Namespace(
        input=input_path, vcf_input=vcf_input, _explicit_dests=explicit
    )
    assert basic._select_vcf_input(args) == expected


@needs_htslib
def test_plain_reference_uses_and_indexes_the_uncompressed_sibling(tmp_path):
    plain = tmp_path / "ref.fa"
    plain.write_text(">chr1\nACGT\n")
    with gzip.open(tmp_path / "ref.fa.gz", "wt") as f:
        f.write(">chr1\nACGT\n")
    with basic.plain_reference(str(tmp_path / "ref.fa.gz"), str(tmp_path)) as ref:
        assert ref == str(plain)
        assert Path(f"{plain}.fai").is_file()
    with basic.plain_reference(str(plain), str(tmp_path)) as ref:
        assert ref == str(plain)


def test_plain_reference_reports_a_corrupt_gzip(tmp_path):
    bad = tmp_path / "ref.fa.gz"
    bad.write_bytes(b"not gzip")
    with pytest.raises(WGSExtractError, match="uncompressed reference"):
        with basic.plain_reference(str(bad), str(tmp_path)):
            pass
    assert [p.name for p in tmp_path.iterdir()] == ["ref.fa.gz"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("#CHROM\tPOS\tRSID\tHG\n", "CHROM,POS,ID,INFO/HG"),
        ("chr\tpos\tother\tid\n", "CHROM,POS,-,ID"),
        ("name\tvalue\n", "ID"),  # no CHROM/POS columns: the default
    ],
)
def test_annotation_columns_from_a_tab_header(tmp_path, header, expected):
    tab = tmp_path / "ann.tab.gz"
    with gzip.open(tab, "wt") as f:
        f.write(header)
    assert basic._annotation_columns(str(tab)) == expected


@needs_htslib
def test_annotation_columns_of_an_unreadable_vcf_default_to_id(tmp_path):
    vcf = tmp_path / "ann.vcf"
    vcf.write_text("not a vcf\n")
    assert basic._annotation_columns(str(vcf)) == "ID"


def _vcf(path: Path, records: list[str], info: str = "") -> str:
    path.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=1000>\n"
        + info
        + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        + "".join(f"{r}\n" for r in records)
    )
    return bgzip_index(path)


@needs_htslib
def test_annotate_copies_ids_and_hg_from_the_annotation_vcf(tmp_path):
    sample = _vcf(
        tmp_path / "sample.vcf",
        ["chr1\t100\t.\tA\tG\t50\tPASS\t.", "chr1\t200\t.\tC\tT\t50\tPASS\t."],
    )
    ann = _vcf(
        tmp_path / "ann.vcf",
        ["chr1\t100\trs1\tA\tG\t.\t.\tHG=R1b"],
        '##INFO=<ID=HG,Number=1,Type=String,Description="Haplogroup">\n',
    )
    outdir = tmp_path / "out"
    outdir.mkdir()
    run_wgsextract(
        "vcf", "annotate", "--input", sample, "--ann-vcf", ann, "--outdir", str(outdir)
    )
    out = outdir / "annotated.vcf.gz"
    query = ["bcftools", "query", "-f", "%POS %ID %INFO/HG\n", str(out)]
    assert run_command(query, capture_output=True).stdout.splitlines() == [
        "100 rs1 R1b",
        "200 . .",
    ]
    assert Path(f"{out}.tbi").is_file()


@needs_htslib
def test_annotate_without_an_annotation_file_fails(tmp_path):
    sample = _vcf(tmp_path / "sample.vcf", ["chr1\t100\t.\tA\tG\t50\tPASS\t."])
    with pytest.raises(WGSExtractError, match="--ann-vcf is required"):
        run_wgsextract("vcf", "annotate", "--input", sample)


def test_annotate_requires_input():
    with pytest.raises(WGSExtractError, match="--input is required"):
        run_wgsextract("vcf", "annotate", "--ann-vcf", "ann.vcf.gz")
