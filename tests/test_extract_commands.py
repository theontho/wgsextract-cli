"""`extract` commands on a tiny BAM: which reads each one keeps, and the
haploid chrM/chrY calls and consensus made from a planted SNP."""

import gzip
import logging
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
from wgsextract_cli.core import dependency_checks
from wgsextract_cli.core.utils import WGSExtractError, run_command

pytestmark = needs_htslib

CONTIGS = ("chr1", "chrX", "chrY", "chrM")
SNP_POS = 300  # every chrY and chrM read carries an ALT base here
READ_STARTS = [201 + 8 * i for i in range(12)]  # all cover SNP_POS


def _alt(base: str) -> str:
    return {"A": "C", "C": "G", "G": "T", "T": "A"}[base]


@pytest.fixture(scope="module")
def genome(tmp_path_factory):
    """A reference and two BAMs: male (reads on every contig, two unmapped) and
    female (no chrY or unmapped reads). Reads are named <contig>_<start>."""
    root = tmp_path_factory.mktemp("extract")
    rng = random.Random(7)
    seqs = {c: "".join(rng.choice("ACGT") for _ in range(1000)) for c in CONTIGS}
    ref = write_fasta(root / "ref.fa", seqs)

    def reads(contig):
        seq = seqs[contig]
        if contig in ("chrY", "chrM"):
            seq = seq[: SNP_POS - 1] + _alt(seq[SNP_POS - 1]) + seq[SNP_POS:]
        return [
            f"{contig}_{s}\t0\t{contig}\t{s}\t60\t100M\t*\t0\t0\t"
            f"{seq[s - 1 : s + 99]}\t{'I' * 100}"
            for s in READ_STARTS
        ]

    header = [f"@SQ\tSN:{c}\tLN:1000" for c in CONTIGS]
    unmapped = [
        f"unmapped_{i}\t4\t*\t0\t0\t*\t*\t0\t0\tACGTACGT\tIIIIIIII" for i in (1, 2)
    ]
    male = sam_to_bam(
        header + [r for c in CONTIGS for r in reads(c)] + unmapped, root / "male.bam"
    )
    female = sam_to_bam(
        header + [r for c in ("chr1", "chrX", "chrM") for r in reads(c)],
        root / "female.bam",
    )
    return {"ref": str(ref), "seqs": seqs, "male": male, "female": female}


def _names(bam) -> set[str]:
    out = run_command(["samtools", "view", str(bam)], capture_output=True).stdout
    return {line.split("\t")[0] for line in out.splitlines()}


def _on(*contigs: str) -> set[str]:
    return {f"{c}_{s}" for c in contigs for s in READ_STARTS}


def _extract(cmd: str, bam: str, outdir: Path, *extra: str) -> None:
    run_wgsextract("extract", cmd, "--input", bam, "--outdir", str(outdir), *extra)


@pytest.mark.parametrize(
    ("cmd", "suffix", "contigs"),
    [
        ("mt-bam", "_mtDNA.bam", ["chrM"]),
        ("ydna-bam", "_Y.bam", ["chrY"]),
        ("y-mt-extract", "_Y_MT.bam", ["chrY", "chrM"]),
    ],
)
def test_contig_extraction_keeps_only_that_contigs_reads(
    genome, tmp_path, caplog, cmd, suffix, contigs
):
    with caplog.at_level(logging.WARNING):
        _extract(cmd, genome["male"], tmp_path)
    out = tmp_path / f"male{suffix}"
    assert _names(out) == _on(*contigs)
    assert Path(f"{out}.bai").is_file()
    assert caplog.text == ""


def test_ydna_bam_of_a_female_sample_warns_it_holds_no_y_dna(genome, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _extract("ydna-bam", genome["female"], tmp_path)
    assert _names(tmp_path / "female_Y.bam") == set()
    assert "appears female" in caplog.text
    assert "has no reads" in caplog.text and "a female sample has none" in caplog.text


def test_unmapped_keeps_only_unmapped_reads(genome, tmp_path):
    _extract("unmapped", genome["male"], tmp_path)
    assert _names(tmp_path / "male_unmapped.bam") == {"unmapped_1", "unmapped_2"}
    assert not list(tmp_path.glob("*.fastq.gz"))


def test_unmapped_fastq_preserves_pairs_single_end_and_singletons(tmp_path):
    common = "\t*\t0\t0\t*\t*\t0\t0\tACGTACGT\tIIIIIIII"
    bam = sam_to_bam(
        [
            "@SQ\tSN:chr1\tLN:1000",
            "pair\t77" + common,
            "pair\t141" + common,
            "orphan\t77" + common,
            "single\t4" + common,
            "mapped\t0\tchr1\t1\t60\t8M\t*\t0\t0\tACGTACGT\tIIIIIIII",
        ],
        tmp_path / "mixed.bam",
    )
    out = tmp_path / "o"
    _extract("unmapped", bam, out, "--fastq", "--threads", "2", "--memory", "256M")
    for suffix, names in [
        ("R1", {"pair"}),
        ("R2", {"pair"}),
        ("SE", {"single"}),
        ("singletons", {"orphan"}),
    ]:
        with gzip.open(out / f"mixed_unmapped_{suffix}.fastq.gz", "rt") as f:
            records = f.read().splitlines()
        assert {name[1:].split("/")[0] for name in records[::4]} == names
    assert _names(out / "mixed_unmapped.bam") == {"pair", "orphan", "single"}


@pytest.mark.parametrize(
    "code,signature",
    [
        ("hs38", None),
        ("hs37-", "88b5f4bbaaec4a097b3aa160bf776093"),
        ("GRCh37", "f8812b68f2aa4aa592e742e081f82807"),
    ],
)
def test_missing_reference_names_the_exact_catalog_install(tmp_path, code, signature):
    from wgsextract_cli.commands.extract import require_reference

    bam = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:248956422"]
        + [f"@SQ\tSN:extra{i}\tLN:100" for i in range(194)]
        + ([f"@CO\tMD5:{signature}"] if signature else []),
        tmp_path / "hs38.bam",
    )
    with pytest.raises(
        WGSExtractError, match=f"wgsextract ref library --install {code}"
    ):
        require_reference(None, "Y extraction", bam)


def test_unmapped_says_when_there_are_none(genome, tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _extract("unmapped", genome["female"], tmp_path)
    assert _names(tmp_path / "female_unmapped.bam") == set()
    assert "has no unmapped reads" in caplog.text


def test_custom_extracts_comma_separated_regions(genome, tmp_path):
    _extract("custom", genome["male"], tmp_path, "-r", "chr1:201-210,chrM:380-388")
    out = tmp_path / "male_chr1_201-210_chrM_380-388.bam"
    # Reads overlapping each region: the first two on chr1, the last two on chrM.
    assert _names(out) == {"chr1_201", "chr1_209", "chrM_281", "chrM_289"}


def test_custom_extracts_bed_regions(genome, tmp_path):
    bed = tmp_path / "targets.bed"
    bed.write_text("chrX\t200\t260\n")
    _extract("custom", genome["male"], tmp_path, "-r", str(bed))
    assert _names(tmp_path / "male_targets.bed.bam") == {
        f"chrX_{s}" for s in READ_STARTS if s <= 260
    }


def test_custom_warns_when_the_region_has_no_reads(genome, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _extract("custom", genome["male"], tmp_path, "-r", "chr1:900-950")
    assert _names(tmp_path / "male_chr1_900-950.bam") == set()
    assert "has no reads: the input has no reads in chr1:900-950" in caplog.text


@pytest.mark.parametrize(
    ("region", "pool"), [([], _on(*CONTIGS)), (["-r", "chrM"], _on("chrM"))]
)
def test_bam_subset_keeps_a_fraction_of_the_reads(genome, tmp_path, region, pool):
    _extract("bam-subset", genome["male"], tmp_path, "--fraction", "0.5", *region)
    kept = _names(tmp_path / "male_subset.bam") - {"unmapped_1", "unmapped_2"}
    assert kept < pool and 0 < len(kept) < len(pool)


@pytest.mark.parametrize(
    ("cmd", "contig", "out"),
    [("mito-vcf", "chrM", "male_MT.vcf.gz"), ("ydna-vcf", "chrY", "male_Y.vcf.gz")],
)
def test_haploid_calling_finds_the_planted_snp(genome, tmp_path, cmd, contig, out):
    _extract(cmd, genome["male"], tmp_path, "--ref", genome["ref"])
    ref_base = genome["seqs"][contig][SNP_POS - 1]
    assert vcf_calls(tmp_path / out) == [
        (contig, str(SNP_POS), ref_base, _alt(ref_base), "1")
    ]
    # The temporary contig BAM is removed; the indexed VCF is all that's left.
    assert sorted(p.name for p in tmp_path.iterdir()) == [out, f"{out}.tbi"]


def test_mito_fasta_is_the_reference_with_the_planted_snp(genome, tmp_path):
    _extract("mito-fasta", genome["male"], tmp_path, "--ref", genome["ref"])
    lines = (tmp_path / "male_MT.fasta").read_text().split()
    seq = genome["seqs"]["chrM"]
    assert lines[0] == ">chrM"
    assert "".join(lines[1:]) == (
        seq[: SNP_POS - 1] + _alt(seq[SNP_POS - 1]) + seq[SNP_POS:]
    )
    assert [p.name for p in tmp_path.iterdir()] == ["male_MT.fasta"]


@pytest.mark.parametrize("cmd", ["mito-fasta", "mito-vcf", "ydna-vcf"])
def test_calling_requires_a_reference(genome, tmp_path, cmd):
    with pytest.raises(WGSExtractError, match="--ref is required"):
        _extract(cmd, genome["male"], tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("cmd", "extra", "error"),
    [
        ("mt-bam", [], "mtDNA extraction failed"),
        ("ydna-bam", [], "Y BAM extraction failed"),
        ("y-mt-extract", [], "Y\\+MT extraction failed"),
        ("custom", ["-r", "chr1:1-100"], "Custom extraction failed"),
        ("bam-subset", ["-f", "0.5", "-r", "chr1"], "BAM subsetting failed"),
        ("mito-vcf", ["--ref", None], "Mito VCF extraction failed"),
        ("mito-fasta", ["--ref", None], "Mito FASTA extraction failed"),
        ("ydna-vcf", ["--ref", None], "Y VCF extraction failed"),
    ],
)
def test_failed_extraction_leaves_no_partial_output(
    genome, tmp_path, cmd, extra, error
):
    # Region queries need the index, so samtools fails after opening its output.
    unindexed = tmp_path / "in" / "male.bam"
    unindexed.parent.mkdir()
    unindexed.write_bytes(Path(genome["male"]).read_bytes())
    outdir = tmp_path / "out"
    extra = [genome["ref"] if arg is None else arg for arg in extra]
    with pytest.raises(WGSExtractError, match=error):
        _extract(cmd, str(unindexed), outdir, *extra)
    assert list(outdir.iterdir()) == []


@pytest.mark.parametrize(
    ("cmd", "tool"), [("mt-bam", "samtools"), ("mito-vcf", "bcftools")]
)
def test_extraction_checks_its_tools_before_starting(
    genome, tmp_path, monkeypatch, cmd, tool
):
    monkeypatch.setattr(
        dependency_checks,
        "check_dependencies",
        lambda tools, *_a: [t for t in tools if t == tool],
    )
    with pytest.raises(WGSExtractError, match=f"Missing required core tool.*{tool}"):
        _extract(cmd, genome["male"], tmp_path, "--ref", genome["ref"])
    assert list(tmp_path.iterdir()) == []
