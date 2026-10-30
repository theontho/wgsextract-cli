"""`microarray` end to end on a tiny library: genotypes, no-calls, sex
chromosomes, --parallel, --region, VCF input and hg38 liftover.

The library has the `ref bootstrap` layout (genomes/<build>.fa and chip
templates), so the genome-wide target is built from the templates. Every marker
is an A in the reference.
"""

import gzip
import random
from pathlib import Path

import pytest

from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

pytestmark = needs_htslib

LENGTH = 5000
CONTIGS = ("chr1", "chrX", "chrY", "chrM")
# rsID, contig, hg19 position.
MARKERS = [
    ("rsHet", "chr1", 1000),
    ("rsAlt", "chr1", 2000),
    ("rsRef", "chr1", 3000),
    ("rsLowMapq", "chr1", 4000),
    ("rsNoReads", "chr1", 4500),
    ("rsX", "chrX", 1000),
    ("rsY", "chrY", 1000),
    ("rsMT", "chrM", 1000),
]
# What a male's reads carry at each covered marker (two letters: alternate reads).
MALE = {
    "rsHet": "AC",
    "rsAlt": "CC",
    "rsRef": "AA",
    "rsLowMapq": "GG",
    "rsX": "GG",
    "rsY": "TT",
    "rsMT": "CC",
}
FEMALE = {"rsHet": "AC", "rsAlt": "CC", "rsRef": "AA", "rsX": "AG", "rsMT": "CC"}
CHROM_23ANDME = {"chr1": "1", "chrX": "X", "chrY": "Y", "chrM": "MT"}


def _genome(seed: int = 3) -> dict[str, str]:
    rng = random.Random(seed)
    seqs = {}
    for contig in CONTIGS:
        seq = [rng.choice("CGT") for _ in range(LENGTH)]
        for _, chrom, pos in MARKERS:
            if chrom == contig:
                seq[pos - 1] = "A"
        seqs[contig] = "".join(seq)
    return seqs


def _templates(root: Path) -> None:
    templates = root / "microarray" / "raw_file_templates"
    (templates / "head").mkdir(parents=True)
    (templates / "body").mkdir()
    (templates / "head" / "23andMe_V3.txt").write_text("# rsid\tchromosome\n")
    (templates / "body" / "23andMe_V3.txt").write_text(
        "".join(f"{r}\t{CHROM_23ANDME[c]}\t{p}\t\n" for r, c, p in MARKERS)
    )
    (templates / "head" / "FTDNA_V2.csv").write_text("RSID,CHROMOSOME\n")
    (templates / "body" / "FTDNA_V2.csv").write_text(
        "".join(f'"{r}","{CHROM_23ANDME[c]}","{p}",\n' for r, c, p in MARKERS)
    )


def _library(root: Path) -> tuple[str, dict[str, str]]:
    seqs = _genome()
    (root / "genomes").mkdir(parents=True)
    fasta = write_fasta(root / "genomes" / "hg19.fa", seqs)
    _templates(root)
    return str(fasta), seqs


def _bam(path: Path, seqs: dict[str, str], genotypes: dict[str, str]) -> str:
    """30 reads over each marker in genotypes; rsLowMapq's have MAPQ 5."""
    lines = [f"@SQ\tSN:{c}\tLN:{LENGTH}" for c in CONTIGS] + ["@RG\tID:s\tSM:s"]
    for rsid, chrom, pos in MARKERS:
        if rsid not in genotypes:
            continue
        mapq = 5 if rsid == "rsLowMapq" else 60
        for i in range(30):
            start = pos - 60 + i
            read = list(seqs[chrom][start - 1 : start + 99])
            read[pos - start] = genotypes[rsid][i % 2]
            lines.append(
                f"{rsid}{i}\t0\t{chrom}\t{start}\t{mapq}\t100M\t*\t0\t0\t"
                f"{''.join(read)}\t{'I' * 100}\tRG:Z:s"
            )
    return sam_to_bam(lines, path)


def _rows(path: Path, sep: str = "\t") -> dict[str, list[str]]:
    rows = {}
    for line in path.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = [p.strip('"') for p in line.split(sep)]
        rows[parts[0]] = parts[1:]
    return rows


def _genotypes(path: Path, sep: str = "\t") -> dict[str, str]:
    return {rsid: parts[-1] for rsid, parts in _rows(path, sep).items()}


@pytest.fixture
def library(tmp_path):
    return _library(tmp_path / "reference")


@pytest.fixture
def quality_library(tmp_path, monkeypatch):
    from wgsextract_cli.core import microarray_targets

    monkeypatch.setattr(microarray_targets, "_FORMATS", ["23andMe_V3", "FTDNA_V2"])
    return _library(tmp_path / "reference")


def _microarray(bam: str, fasta: str, outdir: Path, *extra: str) -> None:
    run_wgsextract(
        "microarray",
        *("--input", bam, "--ref", fasta, "--outdir", str(outdir)),
        *("--formats", "23andme_v3,ftdna_v2", "--threads", "2"),
        *extra,
    )


def test_male_kit_genotypes_and_no_calls(tmp_path, library):
    fasta, seqs = library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)

    _microarray(bam, fasta, tmp_path / "out")

    kit = _genotypes(tmp_path / "out" / "male_23andMe_V3.txt")
    assert {k: v for k, v in kit.items() if k != "rsY"} == {
        "rsHet": "AC",
        "rsAlt": "CC",
        "rsRef": "AA",
        "rsLowMapq": "--",  # MAPQ 5 reads are ignored
        "rsNoReads": "--",
        "rsX": "GG",  # too few X markers to infer a male kit
        "rsMT": "C",  # 23andMe writes haploid calls as one letter
    }
    assert _rows(tmp_path / "out" / "male_23andMe_V3.txt")["rsMT"] == [
        "MT",
        "1000",
        "C",
    ]
    ftdna = _genotypes(tmp_path / "out" / "male_FTDNA_V2.csv", ",")
    assert ftdna == {"RSID": "CHROMOSOME", **kit, "rsMT": "CC", "rsY": "TT"}
    # Positions and chromosome names follow the template, in its order.
    lines = (tmp_path / "out" / "male_FTDNA_V2.csv").read_text().splitlines()
    assert lines[0] == "RSID,CHROMOSOME"
    assert [line.split(",")[0].strip('"') for line in lines[1:]] == [
        r for r, _, _ in MARKERS
    ]
    # The genome-wide target was built from the templates and saved in the library.
    assert (
        tmp_path / "reference" / "microarray" / "All_SNPs_hg19_ref.tab.gz"
    ).is_file()


def test_male_y_markers_are_called(tmp_path, library):
    fasta, seqs = library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)

    _microarray(bam, fasta, tmp_path / "out")

    assert _genotypes(tmp_path / "out" / "male_23andMe_V3.txt")["rsY"] == "T"


def test_female_x_is_diploid_and_y_is_a_no_call(tmp_path, library):
    fasta, seqs = library
    bam = _bam(tmp_path / "female.bam", seqs, FEMALE)

    _microarray(bam, fasta, tmp_path / "out")

    kit = _genotypes(tmp_path / "out" / "female_23andMe_V3.txt")
    assert kit["rsX"] == "AG"
    assert kit["rsY"] == "--"


def test_library_ploidy_file_calls_male_y(tmp_path, library):
    fasta, seqs = library
    (tmp_path / "reference" / "ploidy.txt").write_text("* * * F 2\n* * * M 2\n")
    bam = _bam(tmp_path / "male.bam", seqs, MALE)

    _microarray(bam, fasta, tmp_path / "out")

    assert _genotypes(tmp_path / "out" / "male_23andMe_V3.txt")["rsY"] == "T"


def test_parallel_calling_writes_the_same_kit_in_natural_order(tmp_path, library):
    fasta, seqs = library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)

    _microarray(bam, fasta, tmp_path / "serial")
    _microarray(bam, fasta, tmp_path / "parallel", "--parallel")

    serial = (tmp_path / "serial" / "male_CombinedKit.txt").read_text()
    parallel = (tmp_path / "parallel" / "male_CombinedKit.txt").read_text()
    assert parallel == serial
    chroms = [line.split("\t")[1] for line in serial.splitlines()[1:]]
    assert list(dict.fromkeys(chroms)) == ["1", "X", "Y", "MT"]
    assert not list((tmp_path / "parallel").glob("chrom_chunks_*"))


def test_region_in_the_other_naming_style_limits_the_kit(tmp_path, library):
    fasta, seqs = library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)

    _microarray(bam, fasta, tmp_path / "out", "--region", "X")

    kit = _genotypes(tmp_path / "out" / "male_CombinedKit.txt")
    assert kit == {"rsX": "GG"}


def test_no_reads_at_any_marker_warns_and_writes_no_calls(tmp_path, library, caplog):
    fasta, seqs = library
    bam = _bam(tmp_path / "empty.bam", seqs, {})

    _microarray(bam, fasta, tmp_path / "out")

    assert "No target genotypes were produced" in caplog.text
    kit = _genotypes(tmp_path / "out" / "empty_23andMe_V3.txt")
    assert set(kit.values()) == {"--"}


def test_reason_report_distinguishes_low_mapq_and_no_reads_without_changing_kits(
    tmp_path, quality_library
):
    fasta, seqs = quality_library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)
    _microarray(bam, fasta, tmp_path / "legacy")
    _microarray(bam, fasta, tmp_path / "reasons", "--call-reasons")
    for suffix in ("23andMe_V3.txt", "FTDNA_V2.csv", "CombinedKit.txt"):
        assert (tmp_path / "legacy" / f"male_{suffix}").read_bytes() == (
            tmp_path / "reasons" / f"male_{suffix}"
        ).read_bytes()
    report = _rows(tmp_path / "reasons/male_CombinedKit_reasons.tsv")
    assert report["rsLowMapq"][-2:] == ["--", "low_mapq"]
    assert report["rsNoReads"][-2:] == ["--", "no_reads"]
    assert report["rsHet"][-2:] == ["AC", "called"]


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (("--min-dp", "31"), "low_dp"),
        (("--min-gq", "9999"), "low_gq"),
    ],
)
def test_quality_thresholds_suppress_alignment_calls(
    tmp_path, quality_library, flags, reason, caplog
):
    fasta, seqs = quality_library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)
    _microarray(bam, fasta, tmp_path / "out", *flags, "--call-reasons")
    report = _rows(tmp_path / "out/male_CombinedKit_reasons.tsv")
    assert report["rsHet"][-2:] == ["--", reason]
    assert _genotypes(tmp_path / "out/male_23andMe_V3.txt")["rsHet"] == "--"
    assert "configured quality/FILTER gates" in caplog.text
    assert "the input has no reads at the kit's positions" not in caplog.text


def test_parallel_calling_retains_quality_fields(tmp_path, quality_library):
    fasta, seqs = quality_library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)
    _microarray(
        bam,
        fasta,
        tmp_path / "out",
        "--parallel",
        "--threads",
        "1",
        "--min-dp",
        "31",
        "--call-reasons",
    )
    report = _rows(tmp_path / "out/male_CombinedKit_reasons.tsv")
    assert report["rsHet"][-2:] == ["--", "low_dp"]


def test_a_missing_target_file_is_reported_before_calling(tmp_path, library):
    fasta, seqs = library
    bam = _bam(tmp_path / "male.bam", seqs, MALE)
    missing = str(tmp_path / "targets.tab.gz")

    with pytest.raises(WGSExtractError, match="--ref-vcf-tab not found"):
        _microarray(bam, fasta, tmp_path / "out", "--ref-vcf-tab", missing)
    assert not (tmp_path / "out").exists()


def _vcf(
    path: Path, contigs: tuple[str, ...], records: list[str], extra_headers: str = ""
) -> str:
    path.write_text(
        "##fileformat=VCFv4.2\n"
        + "".join(f"##contig=<ID={c},length={LENGTH}>\n" for c in contigs)
        + '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        + extra_headers
        + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(r.replace(" ", "\t") + "\n" for r in records)
    )
    return bgzip_index(path)


FEMALE_VCF = [
    "1 1000 . A C 50 PASS . GT 0/1",
    "1 2000 . A C 50 PASS . GT 1/1",
    "1 4000 . A G 50 PASS . GT ./.",
    "X 1000 . A G 50 PASS . GT 0/1",
]


def test_vcf_input_fills_absent_targets_with_the_reference(tmp_path, library):
    fasta, _ = library
    vcf = _vcf(tmp_path / "female.vcf", ("1", "X", "Y", "MT"), FEMALE_VCF)

    _microarray(vcf, fasta, tmp_path / "out")

    kit = _genotypes(tmp_path / "out" / "female_23andMe_V3.txt")
    assert {k: v for k, v in kit.items() if k != "rsY"} == {
        "rsHet": "AC",
        "rsAlt": "CC",
        "rsRef": "AA",
        "rsLowMapq": "--",  # a no-call in the VCF stays one
        "rsNoReads": "AA",
        "rsX": "AG",
        "rsMT": "--",  # the VCF has no MT calls at all: unknown, not reference
    }


def test_vcf_input_without_chry_calls_writes_y_no_calls(tmp_path, library):
    fasta, _ = library
    vcf = _vcf(tmp_path / "female.vcf", ("1", "X", "Y", "MT"), FEMALE_VCF)

    _microarray(vcf, fasta, tmp_path / "out")

    assert _genotypes(tmp_path / "out" / "female_23andMe_V3.txt")["rsY"] == "--"


def test_vcf_absent_reference_assumption_can_be_disabled(tmp_path, quality_library):
    fasta, _ = quality_library
    vcf = _vcf(tmp_path / "female.vcf", ("1", "X", "Y", "MT"), FEMALE_VCF)
    _microarray(vcf, fasta, tmp_path / "out", "--no-assume-hom-ref", "--call-reasons")
    report = _rows(tmp_path / "out/female_CombinedKit_reasons.tsv")
    assert report["rsHet"][-2:] == ["AC", "called"]
    assert report["rsRef"][-2:] == ["--", "absent_from_vcf"]
    assert report["rsY"][-2:] == ["--", "absent_contig"]


def test_vcf_quality_gates_use_sample_fields_and_record_filter(
    tmp_path, quality_library
):
    fasta, _ = quality_library
    headers = (
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
        '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Quality">\n'
        '##FILTER=<ID=q10,Description="Filtered">\n'
    )
    records = [
        "1 1000 . A C 50 q10 . GT:DP:GQ 0/1:10:30",
        "1 2000 . A C 50 PASS . GT:DP:GQ 1/1:4:30",
        "1 3000 . A C 50 PASS . GT:DP:GQ 0/1:10:19",
        "1 4000 . A G 50 PASS . GT:DP:GQ 1/1:5:20",
        "1 4500 . A C 50 PASS . GT:DP:GQ 0/1/1:5:20",
    ]
    vcf = _vcf(tmp_path / "quality.vcf", ("1",), records, headers)
    _microarray(
        vcf,
        fasta,
        tmp_path / "out",
        "--min-dp",
        "5",
        "--min-gq",
        "20",
        "--exclude-filtered",
        "--call-reasons",
        "--no-assume-hom-ref",
    )
    report = _rows(tmp_path / "out/quality_CombinedKit_reasons.tsv")
    assert report["rsHet"][-2:] == ["--", "filtered"]
    assert report["rsAlt"][-2:] == ["--", "low_dp"]
    assert report["rsRef"][-2:] == ["--", "low_gq"]
    assert report["rsLowMapq"][-2:] == ["GG", "called"]
    assert report["rsNoReads"][-2:] == ["--", "unsupported_allele"]


def test_vcf_missing_quality_cannot_pass_an_enabled_threshold(
    tmp_path, quality_library
):
    fasta, _ = quality_library
    vcf = _vcf(tmp_path / "female.vcf", ("1", "X", "Y", "MT"), FEMALE_VCF)
    _microarray(vcf, fasta, tmp_path / "out", "--min-gq", "10", "--call-reasons")
    report = _rows(tmp_path / "out/female_CombinedKit_reasons.tsv")
    assert report["rsHet"][-2:] == ["--", "missing_gq"]
    assert report["rsRef"][-2:] == ["--", "missing_gq"]


def test_vcf_input_region_in_the_vcfs_naming_style(tmp_path, library):
    fasta, _ = library
    vcf = _vcf(tmp_path / "female.vcf", ("1", "X", "Y", "MT"), FEMALE_VCF)

    _microarray(vcf, fasta, tmp_path / "out", "--region", "1")

    kit = _genotypes(tmp_path / "out" / "female_CombinedKit.txt")
    # The kit's other chr1 markers lie past the 5 kb test genome (NN).
    assert {k: v for k, v in kit.items() if v != "NN"} == {
        "rsHet": "AC",
        "rsAlt": "CC",
        "rsRef": "AA",
        "rsLowMapq": "--",
        "rsNoReads": "AA",
    }


def test_hg38_input_is_lifted_to_the_hg19_templates(tmp_path):
    # hg38 = hg19 + 100 on every contig: hg38ToHg19 maps [100, 5000) to [0, 4900).
    root = tmp_path / "reference"
    seqs = _genome()
    shifted = {c: "N" * 100 + s[:-100] for c, s in seqs.items()}
    (root / "genomes").mkdir(parents=True)
    fasta = str(write_fasta(root / "genomes" / "hg38.fa", shifted))
    _templates(root)
    with gzip.open(root / "hg38ToHg19.over.chain.gz", "wt") as chain:
        for i, c in enumerate(CONTIGS, 1):
            chain.write(
                f"chain 1 {c} {LENGTH} + 100 {LENGTH} {c} {LENGTH} + 0 "
                f"{LENGTH - 100} {i}\n{LENGTH - 100}\n\n"
            )
    targets = tmp_path / "targets_hg38.tab"
    targets.write_text(
        "#CHROM\tPOS\tID\n" + "".join(f"{c}\t{p + 100}\t{r}\n" for r, c, p in MARKERS)
    )
    run_command([get_tool_path("bgzip"), "-f", str(targets)])
    targets_gz = f"{targets}.gz"
    run_command([get_tool_path("tabix"), "-s", "1", "-b", "2", "-e", "2", targets_gz])
    hg38_markers = [(r, c, p + 100) for r, c, p in MARKERS]
    lines = [f"@SQ\tSN:{c}\tLN:{LENGTH}" for c in CONTIGS] + ["@RG\tID:s\tSM:s"]
    for rsid, chrom, pos in hg38_markers:
        if rsid in ("rsHet", "rsAlt"):
            for i in range(30):
                start = pos - 60 + i
                read = list(shifted[chrom][start - 1 : start + 99])
                read[pos - start] = MALE[rsid][i % 2]
                lines.append(
                    f"{rsid}{i}\t0\t{chrom}\t{start}\t60\t100M\t*\t0\t0\t"
                    f"{''.join(read)}\t{'I' * 100}\tRG:Z:s"
                )
    bam = sam_to_bam(lines, tmp_path / "sample.bam")

    _microarray(bam, fasta, tmp_path / "out", "--ref-vcf-tab", targets_gz)

    out = tmp_path / "out"
    assert _rows(out / "sample_CombinedKit.txt")["rsHet"] == ["1", "1100", "AC"]
    assert _rows(out / "sample_CombinedKit_hg19.txt")["rsHet"] == ["1", "1000", "AC"]
    kit = _rows(out / "sample_23andMe_V3.txt")
    assert kit["rsHet"] == ["1", "1000", "AC"]
    assert kit["rsAlt"] == ["1", "2000", "CC"]
    assert kit["rsRef"] == ["1", "3000", "--"]
