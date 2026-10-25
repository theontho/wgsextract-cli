"""vcf to-fasta: exact consensus sequences from real bcftools on tiny genomes."""

import gzip
import logging
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, run_wgsextract
from wgsextract_cli.commands.vcf import consensus
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = needs_htslib

# 1-based position p holds "ACGT"[(p - 1) % 4]: T at 12 and 16, C at 10.
REF = "ACGT" * 50


def _vcf(path: Path, body: str, contigs=("chr1",), samples=("S",)) -> str:
    text = (
        "##fileformat=VCFv4.2\n"
        + "".join(f"##contig=<ID={c},length=200>\n" for c in contigs)
        + '##FILTER=<ID=FAIL,Description="f">\n'
        '##INFO=<ID=END,Number=1,Type=Integer,Description="e">\n'
        '##INFO=<ID=SVTYPE,Number=1,Type=String,Description="s">\n'
        '##ALT=<ID=DEL,Description="d">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
        + "".join(f"\t{s}" for s in ("FORMAT", *samples) if samples)
        + "\n"
        + body
    )
    plain = path.with_suffix("")
    plain.write_text(text)
    return bgzip_index(plain, path)


def _to_fasta(tmp_path: Path, body: str, *argv: str, contigs=("chr1",), ref=None):
    """Run `vcf to-fasta` and return {header: sequence} of the consensus."""
    ref_path = tmp_path / "ref.fa"
    ref_path.write_text("".join(f">{c}\n{REF}\n" for c in ref or ("chr1",)))
    vcf = _vcf(tmp_path / "v.vcf.gz", body, contigs)
    out = tmp_path / "out"
    run_wgsextract(
        "vcf", "to-fasta", "--input", vcf, "--ref", str(ref_path),
        "--outdir", str(out), *argv,
    )  # fmt: skip
    seqs: dict[str, str] = {}
    with gzip.open(out / "v_consensus.fa.gz", "rt") as f:
        for line in f:
            if line.startswith(">"):
                name = line[1:].strip()
                seqs[name] = ""
            else:
                seqs[name] += line.strip()
    return seqs


def _rec(pos, ref, alt, gt, flt="PASS", chrom="chr1", info="."):
    return f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t50\t{flt}\t{info}\tGT\t{gt}\n"


def _with(pos: int, bases: str, drop: int = 1, seq: str = REF) -> str:
    """``seq`` with ``drop`` bases at 1-based ``pos`` replaced by ``bases``."""
    return seq[: pos - 1] + bases + seq[pos - 1 + drop :]


def test_multiallelic_het_becomes_two_alt_iupac_code(tmp_path):
    seqs = _to_fasta(tmp_path, _rec(12, "T", "C,G", "1/2"))
    assert seqs == {"chr1": _with(12, "S")}


def test_homozygous_indels_shift_sequence_and_chain(tmp_path):
    body = _rec(12, "TA", "T", "1/1") + _rec(20, "T", "TGG", "1/1")
    seqs = _to_fasta(tmp_path, body, "--chain")
    assert seqs == {"chr1": _with(12, "T", 2, seq=_with(20, "TGG"))}
    chain = (tmp_path / "out" / "v_consensus.chain").read_text().split("\n")
    # Reference 200 bp -> consensus 201 bp: 12 bp, 1 deleted, 7 bp, 2 inserted.
    assert chain[0].split()[2:] == "chr1 200 + 0 200 chr1 201 + 0 201 1".split()
    assert chain[1:4] == ["12 1 0", "7 0 2", "180"]


@pytest.mark.parametrize(
    ("haplotype", "expected"),
    [
        ("1", _with(12, "C")),  # 1|0 applies; 0|1 and unphased 0/1 keep REF
        ("2", _with(20, "C", seq=_with(16, "G"))),
        ("alt", _with(20, "C", seq=_with(16, "G", seq=_with(12, "C")))),
        ("iupac", _with(20, "Y", seq=_with(16, "K", seq=_with(12, "Y")))),
    ],
)
def test_haplotype_choice_picks_alleles(tmp_path, haplotype, expected):
    body = (
        _rec(12, "T", "C", "1|0")
        + _rec(16, "T", "G", "0|1")
        + _rec(20, "T", "C", "0/1")
    )
    assert _to_fasta(tmp_path, body, "--haplotype", haplotype) == {"chr1": expected}


def test_uncalled_n_keeps_only_called_reference_and_masks_no_calls(tmp_path):
    body = _rec(12, "T", "C", "0/0") + _rec(16, "T", "G", "./.")
    seqs = _to_fasta(tmp_path, body, "--uncalled", "n", "--region", "chr1:1-20")
    assert seqs == {"chr1:1-20": "N" * 11 + "T" + "N" * 8}


def test_gvcf_no_call_block_is_masked_whole(tmp_path):
    body = _rec(1, "A", "<*>", "./.", ".", info="END=50") + _rec(
        51, "G", "<*>", "0/0", ".", info="END=200"
    )
    seqs = _to_fasta(tmp_path, body, "--region", "chr1:1-60")
    assert seqs == {"chr1:1-60": "N" * 50 + REF[50:60]}


def test_failed_gvcf_call_is_masked_unless_filtered_calls_are_included(tmp_path):
    body = (
        _rec(1, "A", "<*>", "0/0", ".", info="END=10")
        + _rec(11, "G", "A", "1/1", "FAIL")
        + _rec(12, "T", "<*>", "0/0", ".", info="END=200")
    )
    seqs = _to_fasta(tmp_path, body, "--region", "chr1:1-20")
    assert seqs == {"chr1:1-20": _with(11, "N")[:20]}
    seqs = _to_fasta(tmp_path, body, "--region", "chr1:1-20", "--include-filtered")
    assert seqs == {"chr1:1-20": _with(11, "A")[:20]}


@pytest.mark.parametrize("region", [None, "chr1:1-40"])
def test_user_mask_and_gvcf_uncalled_mask_both_apply(tmp_path, region):
    (tmp_path / "m.bed").write_text("chr1\t20\t30\n")
    body = _rec(1, "A", "<*>", "0/0", ".", info="END=180") + _rec(192, "T", "C", "1/1")
    argv = ["--mask", str(tmp_path / "m.bed")] + (
        ["--region", region] if region else []
    )
    seqs = _to_fasta(tmp_path, body, *argv)
    whole = REF[:20] + "N" * 10 + REF[30:180] + "N" * 11 + "C" + "N" * 8
    assert seqs == ({"chr1": whole} if not region else {region: whole[:40]})


def test_vcf_contigs_are_renamed_to_reference_naming(tmp_path):
    body = _rec(12, "T", "C", "1/1", chrom="MT")
    seqs = _to_fasta(tmp_path, body, contigs=("MT",), ref=("chr1", "chrM"))
    assert seqs == {"chr1": REF, "chrM": _with(12, "C")}
    seqs = _to_fasta(
        tmp_path, body, "--region", "MT:1-20", contigs=("MT",), ref=("chr1", "chrM")
    )
    assert seqs == {"chrM:1-20": _with(12, "C")[:20]}


def test_vcf_contigs_missing_from_reference_are_skipped(tmp_path, caplog):
    body = _rec(12, "T", "C", "1/1") + _rec(12, "T", "C", "1/1", chrom="HLA-A*01:01")
    with caplog.at_level(logging.WARNING):
        seqs = _to_fasta(tmp_path, body, contigs=("chr1", "HLA-A*01:01"))
    assert seqs == {"chr1": _with(12, "C")}
    assert "HLA-A*01:01" in caplog.text


def test_vcf_without_reference_contigs_is_rejected(tmp_path):
    with pytest.raises(WGSExtractError, match="different reference|contigs"):
        _to_fasta(tmp_path, _rec(12, "T", "C", "1/1", chrom="chrZ"), contigs=("chrZ",))


def test_region_outside_vcf_is_reference_and_unknown_contig_fails(tmp_path):
    seqs = _to_fasta(
        tmp_path, _rec(12, "T", "C", "1/1"), "--region", "chr2:1-20",
        ref=("chr1", "chr2"),
    )  # fmt: skip
    assert seqs == {"chr2:1-20": REF[:20]}
    with pytest.raises(WGSExtractError, match="'chr9' is not in the reference"):
        _to_fasta(tmp_path, _rec(12, "T", "C", "1/1"), "--region", "chr9:1-20")


def test_unknown_sample_and_sites_only_vcf_are_rejected(tmp_path):
    with pytest.raises(WGSExtractError, match="'X' is not in"):
        _to_fasta(tmp_path, _rec(12, "T", "C", "1/1"), "--sample", "X")
    vcf = _vcf(tmp_path / "s.vcf.gz", "chr1\t12\t.\tT\tC\t50\tPASS\t.\n", samples=())
    with pytest.raises(WGSExtractError, match="no genotype columns"):
        consensus._pick_sample(vcf, None)


def test_missing_reference_fails_before_writing(tmp_path):
    vcf = _vcf(tmp_path / "v.vcf.gz", _rec(12, "T", "C", "1/1"))
    with pytest.raises(WGSExtractError, match="consensus generation"):
        run_wgsextract("vcf", "to-fasta", "--input", vcf, "--outdir", str(tmp_path))
    assert not list(tmp_path.glob("*consensus*"))


def test_failure_while_writing_removes_partial_outputs(tmp_path, monkeypatch):
    def fail(stages, stdout=None, description=""):
        stdout.write(b"partial")
        Path(tmp_path / "out" / "v_consensus.chain").write_text("partial")
        raise WGSExtractError("consensus died")

    monkeypatch.setattr(consensus, "run_pipeline", fail)
    (tmp_path / "out").mkdir()
    with pytest.raises(WGSExtractError, match="consensus died"):
        _to_fasta(tmp_path, _rec(12, "T", "C", "1/1"), "--chain", contigs=("chr1",))
    assert list((tmp_path / "out").iterdir()) == []


def test_is_gvcf_ignores_structural_variant_ends(tmp_path):
    sv = _rec(12, "T", "<DEL>", "1/1", info="SVTYPE=DEL;END=40")
    assert not consensus.is_gvcf(_vcf(tmp_path / "sv.vcf.gz", sv))
    block = _rec(1, "A", "C,<NON_REF>", "0/1", info="END=1")
    assert consensus.is_gvcf(_vcf(tmp_path / "g.vcf.gz", block))


def test_uncalled_mask_lists_gaps_between_overlapping_records(tmp_path):
    body = (
        _rec(5, "A", "<*>", "0/0", info="END=10")
        + _rec(8, "TACG", "T", "1/1")  # deletion covers 8-11
        + _rec(20, "T", "C", "1/1", "FAIL")
    )
    vcf = _vcf(tmp_path / "v.vcf.gz", body, contigs=("chr1", "chr2"))
    lengths = {"chr1": 30, "chr2": 7}
    bed = tmp_path / "u.bed"
    consensus.write_uncalled_mask(vcf, lengths, str(bed))
    assert bed.read_text().splitlines() == [
        "chr1\t0\t4", "chr1\t11\t19", "chr1\t20\t30", "chr2\t0\t7",
    ]  # fmt: skip
    consensus.write_uncalled_mask(vcf, lengths, str(bed), "chr1:3-25", 'FILTER="PASS"')
    assert bed.read_text().splitlines() == ["chr1\t2\t4", "chr1\t11\t25"]
    with pytest.raises(WGSExtractError, match="Listing called positions failed"):
        consensus.write_uncalled_mask(str(tmp_path / "none.vcf.gz"), lengths, str(bed))


@pytest.mark.parametrize(
    ("record_ref", "alt", "fasta", "expected"),
    [
        ("N", "NTT", "R", ("R", "RTT")),
        ("NC", "A,NG", "RC", ("RC", "A,RG")),
        ("A", "G", "R", None),  # a genuine mismatch is left to fail
        ("N", "G", "RC", None),  # lengths differ
        ("R", "G", "R", None),  # already matches
    ],
)
def test_fixed_ref_record_only_resolves_n_bases(record_ref, alt, fasta, expected):
    line = f"chr1\t9\t.\t{record_ref}\t{alt}\t50\tPASS\t.\n"
    fixed = consensus._fixed_ref_record(line, fasta)
    assert (fixed and tuple(fixed.split("\t")[3:5])) == expected


def test_region_with_thousands_separators_applies_variants(tmp_path):
    seqs = _to_fasta(tmp_path, _rec(12, "T", "C", "1/1"), "--region", "chr1:1-1,50")
    assert list(seqs.values()) == [_with(12, "C")[:150]]


def test_region_without_end_runs_to_the_contig_end(tmp_path):
    seqs = _to_fasta(tmp_path, _rec(12, "T", "C", "1/1"), "--region", "chr1:12")
    assert list(seqs.values()) == ["C" + REF[12:]]


def test_iupac_keeps_reference_at_heterozygous_deletion(tmp_path):
    seqs = _to_fasta(tmp_path, _rec(12, "TA", "T", "0/1"))
    assert seqs == {"chr1": REF}
