"""Microarray regressions from PR 43: chromosome aliases (chr1/1, chrM/MT),
SNP-shaped genotypes and target renaming, on tiny files with real htslib."""

import gzip
import io
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli.commands import _microarray_combined, _microarray_vcf, microarray
from wgsextract_cli.core import variant_files
from wgsextract_cli.core.builds import ploidy_for_build
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.microarray_utils import (
    liftover_hg38_to_hg19,
    write_formatted_line,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command

LENGTH = 400
GENOME = "".join(random.Random(43).choice("ACGT") for _ in range(LENGTH))


@pytest.mark.parametrize(
    ("build", "expected_ploidy"),
    [
        ("GRCh37", "GRCh37"),
        ("hs37", "GRCh37"),
        ("GRCh38", "GRCh38"),
        ("hs38DH", "GRCh38"),
        ("hs38d1", "GRCh38"),
    ],
)
def test_reference_model_aliases_get_their_builds_sex_aware_ploidy(
    build, expected_ploidy
):
    assert ploidy_for_build(build) == expected_ploidy


@pytest.mark.parametrize(
    ("format_key", "expected"),
    [
        ("all", None),
        ("combined-all", None),
        ("23andme_v3+v5", "23andMe_V35"),
        ("23andme-v3-v5", "23andMe_V35"),
        ("23andMe_V35", "23andMe_V35"),
        ("23andme-v5", "23andMe_V5"),
        ("familytreedna-v2", "FTDNA_V2"),
        ("livingdna-v1", "LDNA_V1"),
        ("myheritage-v2", "MyHeritage_V2"),
        ("23andme-v5-2025", "23andMe_V5_2025"),
        ("ancestry_v2_2024", "Ancestry_V2_2024"),
        ("nucleus", "Nucleus_V1"),
        ("My-Custom-Format", "my_custom_format"),
    ],
)
def test_microarray_format_aliases_match_template_names(format_key, expected):
    assert microarray._resolve_microarray_format(format_key) == expected


def test_convert_microarray_outputs_preserves_vendor_wgsextract_error(tmp_path):
    combined_kit = tmp_path / "sample_CombinedKit.txt"
    ref_fasta = tmp_path / "ref.fa"
    combined_kit.write_text("rs1\t1\t100\tAA\n")
    ref_fasta.touch()
    args = SimpleNamespace(formats="23andme_v5")
    lib = SimpleNamespace(root=str(tmp_path), build="GRCh37")

    with patch(
        "wgsextract_cli.core.microarray_utils.convert_to_vendor_format",
        side_effect=WGSExtractError("Template body not found: specific-template"),
    ):
        with pytest.raises(WGSExtractError, match="specific-template"):
            microarray._convert_microarray_outputs(
                args=args,
                outdir=str(tmp_path),
                base_name="sample",
                lib=lib,
                combined_kit_txt=str(combined_kit),
                ref_fasta=str(ref_fasta),
                start_total=0.0,
            )


def _vcf(path: Path, contigs: tuple[str, ...], records: list[str]) -> str:
    """Write a plain single-sample VCF; records are space-separated columns."""
    path.write_text(
        "##fileformat=VCFv4.2\n"
        + "".join(f"##contig=<ID={c},length={LENGTH}>\n" for c in contigs)
        + '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(r.replace(" ", "\t") + "\n" for r in records)
    )
    return str(path)


def _targets(path: Path, rows: list[str], header: str = "#CHROM\tPOS\tID") -> str:
    path.write_text(header + "\n" + "".join(r.replace(" ", "\t") + "\n" for r in rows))
    return str(path)


def _indexed_targets(path: Path, rows: list[str]) -> str:
    """A bgzipped, tabix-indexed CHROM/POS/ID/REF target tab, as libraries ship."""
    plain = path.with_suffix("")
    plain.write_text("".join(r.replace(" ", "\t") + "\n" for r in rows))
    run_command([get_tool_path("bgzip"), "-f", str(plain)])
    run_command([get_tool_path("tabix"), "-f", "-s1", "-b2", "-e2", str(path)])
    return str(path)


def _kit(tmp_path: Path, *, is_vcf: bool, out_vcf: str, fasta: str, targets: str):
    combined_kit = _microarray_combined._write_microarray_combined_kit(
        args=SimpleNamespace(region=None),
        outdir=str(tmp_path),
        base_name="sample",
        is_vcf=is_vcf,
        out_vcf=out_vcf,
        ref_fasta=fasta,
        ref_vcf_tab=targets,
    )
    return Path(combined_kit).read_text().splitlines()[1:]


@needs_htslib
def test_combined_kit_vcf_mode_preserves_existing_mt_chromosome(tmp_path):
    fasta = write_fasta(tmp_path / "ref.fa", {"MT": GENOME})
    # The VCF has MT calls, just not at the target, so the target is reference.
    vcf = _vcf(tmp_path / "hits.vcf", ("MT",), ["MT 150 . C G 50 PASS . GT 1/1"])
    targets = _targets(tmp_path / "t.tsv", ["MT 100 rsMT A"], "#CHROM\tPOS\tID\tREF")

    rows = _kit(tmp_path, is_vcf=True, out_vcf=vcf, fasta=str(fasta), targets=targets)

    assert rows == ["rsMT\tMT\t100\tAA"]


@needs_htslib
def test_combined_kit_vcf_mode_matches_mito_aliases_for_hits_and_reference(tmp_path):
    fasta = write_fasta(tmp_path / "ref.fa", {"chrM": GENOME})
    vcf = _vcf(tmp_path / "hits.vcf", ("chrM",), ["chrM 100 . G A 50 PASS . GT 1/1"])
    targets = _targets(tmp_path / "t.tsv", ["MT 100 rsHit", "MT 101 rsRef"])

    rows = _kit(tmp_path, is_vcf=True, out_vcf=vcf, fasta=str(fasta), targets=targets)

    assert rows == [
        "rsHit\tMT\t100\tAA",
        f"rsRef\tMT\t101\t{GENOME[100] * 2}",  # chrM's base, read through the alias
    ]


@needs_htslib
def test_combined_kit_vcf_mode_emits_snp_shaped_genotypes(tmp_path):
    seq = list(GENOME)
    seq[102] = "T"  # rsRef
    seq[103] = "Y"  # rsAmbigRef: an IUPAC base can't be a genotype
    fasta = write_fasta(tmp_path / "ref.fa", {"chr1": "".join(seq)})
    vcf = _vcf(
        tmp_path / "hits.vcf",
        ("chr1",),
        [
            "chr1 100 . C . 50 PASS . GT 0",  # haploid call
            "chr1 100 . C CT 50 PASS . GT 0/1",  # an indel at the same site
            "chr1 101 . G . 50 PASS . GT .",
            "chr1 102 . A T 50 PASS . GT 0/1",
            "chr1 105 . T C 50 PASS . GT 0/1",
        ],
    )
    targets = _targets(
        tmp_path / "t.tsv",
        [
            "chr1 100 rsHom",
            "chr1 101 rsNoCall",
            "chr1 102 rsHet",
            "chr1 103 rsRef",
            "chr1 104 rsAmbigRef",
            "chr1 105 rsHetAltFirst",
        ],
    )

    rows = _kit(tmp_path, is_vcf=True, out_vcf=vcf, fasta=str(fasta), targets=targets)

    assert rows == [
        "rsHom\t1\t100\tCC",
        "rsNoCall\t1\t101\t--",
        "rsHet\t1\t102\tAT",
        "rsRef\t1\t103\tTT",
        "rsAmbigRef\t1\t104\tNN",
        "rsHetAltFirst\t1\t105\tCT",  # alphabetical, as chips write
    ]


@needs_htslib
def test_combined_kit_bam_mode_normalizes_mito_chromosome_names(tmp_path):
    vcf = _vcf(
        tmp_path / "calls.vcf",
        ("MT", "M", "chrM", "chr1"),
        [
            "MT 16 rsMT A . 50 PASS . GT 0/0",
            "M 1 rsM C . 50 PASS . GT 0/0",
            "chrM 2 rsChrM G . 50 PASS . GT 0/0",
            "chr1 3 rs1 T . 50 PASS . GT 0/0",
        ],
    )

    rows = _kit(tmp_path, is_vcf=False, out_vcf=vcf, fasta="", targets="")

    assert rows == [
        "rsMT\tMT\t16\tAA",
        "rsM\tMT\t1\tCC",
        "rsChrM\tMT\t2\tGG",
        "rs1\t1\t3\tTT",
    ]


def test_23andme_writer_does_not_expand_existing_mt_chromosome():
    out = io.StringIO()

    write_formatted_line(out, "23andMe_V3", "rsMT", "MT", "100", "AA")
    write_formatted_line(out, "23andMe_V3", "rsM", "M", "101", "CC")

    assert out.getvalue().splitlines() == [
        "rsMT\tMT\t100\tAA",
        "rsM\tMT\t101\tCC",
    ]


def test_liftover_accepts_prefixed_chromosome_names(tmp_path):
    chain_file = tmp_path / "hg38ToHg19.over.chain.gz"
    chain_file.touch()
    input_txt = tmp_path / "input.txt"
    output_txt = tmp_path / "output.txt"
    input_txt.write_text(
        "rsChrM\tchrM\t100\tAA\nrsChr1\tchr1\t200\tCC\nrsMT\tMT\t300\tGG\n"
    )
    seen_chroms = []

    class FakeLiftOver:
        def __init__(self, chain_path):
            assert chain_path == str(chain_file)

        def convert_coordinate(self, chrom, pos):
            seen_chroms.append(chrom)
            return [(chrom, pos + 10)]

    with patch("wgsextract_cli.core.microarray_coordinates.LiftOver", FakeLiftOver):
        liftover_hg38_to_hg19(str(input_txt), str(output_txt), str(chain_file))

    rows = {
        parts[0]: parts
        for parts in (
            line.split("\t") for line in output_txt.read_text().strip().splitlines()
        )
    }
    assert seen_chroms == ["chrM", "chr1", "chrM"]
    assert rows["rsChrM"] == ["rsChrM", "MT", "110", "AA"]
    assert rows["rsChr1"] == ["rsChr1", "1", "210", "CC"]
    assert rows["rsMT"] == ["rsMT", "MT", "310", "GG"]
    assert all(parts[1] != "MTT" for parts in rows.values())


def test_chromosome_rename_mapping_matches_mito_aliases():
    assert variant_files.chromosome_rename_mapping(
        ["chr1", "chrM", "chrUn"], ["1", "MT"]
    ) == [("chr1", "1"), ("chrM", "MT")]
    assert variant_files.chromosome_rename_mapping(["MT"], ["chrM"]) == [("MT", "chrM")]


@pytest.mark.parametrize(
    "contig", ["11_KI270721v1_random", "Un_KI270302v1", "6_GL000250v2_alt"]
)
def test_chromosome_rename_mapping_matches_nonprimary_contigs_in_both_directions(
    contig,
):
    assert variant_files.chromosome_rename_mapping([contig], [f"chr{contig}"]) == [
        (contig, f"chr{contig}")
    ]
    assert variant_files.chromosome_rename_mapping([f"chr{contig}"], [contig]) == [
        (f"chr{contig}", contig)
    ]


@needs_htslib
def test_normalize_vcf_chromosomes_renames_chr_m_to_target_mt(tmp_path):
    vcf = bgzip_index(
        _vcf(
            tmp_path / "input.vcf",
            ("chr1", "chrM"),
            ["chr1 5 . A C 50 PASS . GT 0/1", "chrM 7 . G T 50 PASS . GT 1/1"],
        )
    )

    normalized = variant_files.normalize_vcf_chromosomes(vcf, ["1", "MT"])

    assert vcf_calls(normalized) == [
        ("1", "5", "A", "C", "0/1"),
        ("MT", "7", "G", "T", "1/1"),
    ]
    # The output may become a final file: it mustn't keep a 0600 mkstemp mode.
    plain = tmp_path / "plain"
    plain.touch()
    assert Path(normalized).stat().st_mode == plain.stat().st_mode


@needs_htslib
def test_microarray_vcf_targets_normalize_to_input_vcf_chromosomes(tmp_path):
    targets = _indexed_targets(
        tmp_path / "targets.tab.gz",
        ["chr1 100 rs1 A", "chrM 200 rsM C", "chrUn 300 rsUn G"],
    )

    normalized = _microarray_vcf._prepare_vcf_target_tab_for_input(
        targets, str(tmp_path / "input.vcf.gz"), str(tmp_path), ["1", "MT"]
    )

    with gzip.open(normalized, "rt") as handle:
        assert handle.read().splitlines() == ["1\t100\trs1\tA", "MT\t200\trsM\tC"]
    tabix = [get_tool_path("tabix"), "-l", normalized]
    assert run_command(tabix, capture_output=True).stdout.split() == ["1", "MT"]


def _covering_reads(chrom: str, seq: str, pos: int, base: str) -> list[str]:
    """20 reads with ``base`` at 1-based ``pos``."""
    reads = []
    for i in range(20):
        start = pos - 40 + i
        read = list(seq[start - 1 : start + 59])
        read[pos - start] = base
        reads.append(
            f"{chrom}_{pos}_{i}\t0\t{chrom}\t{start}\t60\t60M\t*\t0\t0\t"
            f"{''.join(read)}\t{'I' * 60}\tRG:Z:s"
        )
    return reads


@needs_htslib
def test_microarray_alignment_targets_normalize_to_input_chromosomes(tmp_path):
    seqs = {"chr1": GENOME, "chrM": GENOME[::-1]}
    fasta = write_fasta(tmp_path / "ref.fa", seqs)
    alt = {"A": "C", "C": "G", "G": "T", "T": "A"}
    bam = sam_to_bam(
        [f"@SQ\tSN:{c}\tLN:{LENGTH}" for c in seqs]
        + ["@RG\tID:s\tSM:s"]
        + _covering_reads("chr1", GENOME, 100, alt[GENOME[99]])
        + _covering_reads("chrM", seqs["chrM"], 200, alt[seqs["chrM"][199]]),
        tmp_path / "sample.bam",
    )
    targets = _indexed_targets(
        tmp_path / "targets.tab.gz",
        [f"1 100 rs1 {GENOME[99]}", f"MT 200 rsM {seqs['chrM'][199]}"],
    )

    out_vcf, normalized = _microarray_vcf._prepare_microarray_vcf(
        args=SimpleNamespace(input=bam, parallel=False, region=None),
        outdir=str(tmp_path),
        base_name="sample",
        is_vcf=False,
        ref_vcf_tab=targets,
        region_args=[],
        ploidy_args=["--ploidy", "2"],
        ref_fasta=str(fasta),
        threads="1",
        start_vcf=0.0,
        out_vcf=str(tmp_path / "sample_combined.vcf.gz"),
    )

    with gzip.open(normalized, "rt") as handle:
        assert [line.split("\t")[0] for line in handle] == ["chr1", "chrM"]
    query = [get_tool_path("bcftools"), "query", "-f", "%ID %CHROM %POS [%GT]\n"]
    calls = run_command([*query, out_vcf], capture_output=True).stdout.splitlines()
    assert calls == ["rs1 chr1 100 1/1", "rsM chrM 200 1/1"]


@needs_htslib
def test_microarray_vcf_returns_original_targets_for_reference_filling(tmp_path):
    vcf = bgzip_index(
        _vcf(
            tmp_path / "sample.vcf",
            ("1", "2"),
            ["1 100 . A C 50 PASS . GT 0/1", "2 100 . A C 50 PASS . GT 0/1"],
        )
    )
    targets = _indexed_targets(
        tmp_path / "targets.tab.gz", ["chr1 100 rs1 A", "chrUn 300 rsUn G"]
    )

    out_vcf, combined_targets = _microarray_vcf._prepare_microarray_vcf(
        args=SimpleNamespace(input=vcf, parallel=False, region=None),
        outdir=str(tmp_path),
        base_name="sample",
        is_vcf=True,
        ref_vcf_tab=targets,
        region_args=[],
        ploidy_args=["--ploidy", "2"],
        ref_fasta=str(tmp_path / "ref.fa"),
        threads="1",
        start_vcf=0.0,
        out_vcf=str(tmp_path / "sample_combined.vcf.gz"),
    )

    # The kit fills absent targets from the full target list, not the renamed one.
    assert combined_targets == targets
    query = [get_tool_path("bcftools"), "query", "-f", "%ID %CHROM %POS\n", out_vcf]
    assert run_command(query, capture_output=True).stdout.splitlines() == ["rs1 1 100"]


@needs_htslib
def test_vcf_index_chromosomes_returns_index_contigs(tmp_path):
    vcf = bgzip_index(
        _vcf(
            tmp_path / "input.vcf",
            ("1", "2", "MT"),
            ["1 5 . A C 50 PASS . GT 0/1", "MT 7 . G T 50 PASS . GT 1/1"],
        )
    )

    assert variant_files.vcf_index_chromosomes(vcf) == ["1", "MT"]


def test_microarray_vcf_region_normalizes_to_input_vcf_chromosome_style():
    assert _microarray_vcf._normalize_region_args_for_input(
        ["-r", "chrM:1-200"], ["1", "MT"]
    ) == ["-r", "MT:1-200"]
    assert _microarray_vcf._normalize_region_args_for_input(
        ["-r", "chr1"], ["1", "MT"]
    ) == ["-r", "1"]
