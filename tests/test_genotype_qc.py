"""Low-coverage and imputation warnings: depth from the info cache, CRAM
container headers or a BAM index, and genotype rates from written kits."""

import json
import logging
import random
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli.core import genotype_qc
from wgsextract_cli.core.genotype_qc import (
    AUTOSOMES,
    CHR_X,
    CHR_Y,
    MTDNA,
    region_targets,
    warn_about_genotypes,
    warn_low_depth,
)
from wgsextract_cli.core.microarray_utils import GenotypeTally
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.main import main

LENGTHS = {"chr1": 2000, "chrX": 2000, "chrY": 1000, "chrM": 500}
# 50 bp reads per contig: chr1 2.5x, chrX 1.25x, chrY 2x, chrM 20x.
READS = {"chr1": 100, "chrX": 50, "chrY": 40, "chrM": 200}


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    root = tmp_path_factory.mktemp("qc")
    rng = random.Random(3)
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(n)) for c, n in LENGTHS.items()
    }
    ref = write_fasta(root / "ref.fa", seqs)
    reads = []
    for contig, n in READS.items():
        for i in range(n):
            pos = 1 + (i * 37) % (LENGTHS[contig] - 50)
            seq = seqs[contig][pos - 1 : pos + 49]
            reads.append(
                f"r{contig}{i}\t0\t{contig}\t{pos}\t60\t50M\t*\t0\t0\t{seq}\t{'I' * 50}"
            )
    header = [f"@SQ\tSN:{c}\tLN:{n}" for c, n in LENGTHS.items()]
    bam = sam_to_bam(header + reads, root / "s.bam")
    return {"root": root, "ref": str(ref), "bam": bam}


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_region_targets():
    assert region_targets(None) == {AUTOSOMES}
    assert region_targets("chrY") == {CHR_Y}
    assert region_targets("chrM:1-100,X") == {MTDNA, CHR_X}
    assert region_targets("chr20:1-5,000") == {AUTOSOMES}
    assert region_targets("chr20:1,500,000-1,600,000 chrY") == {AUTOSOMES, CHR_Y}
    assert region_targets("chrUn_KI270302v1") == set()


@needs_htslib
def test_bam_index_depth_warns_per_target_threshold(sample, caplog):
    caplog.set_level(logging.INFO)
    depths = warn_low_depth(sample["bam"], set(genotype_qc.TARGETS), ref=sample["ref"])
    # Low targets are measured again over sampled windows with reads.
    assert depths == pytest.approx(
        {AUTOSOMES: 2.5, CHR_X: 1.25, CHR_Y: 2.0, MTDNA: 20.0}, rel=0.1
    )
    warnings = _warnings(caplog)
    # Male (chrY > a quarter of chrX), so chrX is held to the haploid 3x.
    assert [w.split(" depth")[0].split()[-1] for w in warnings] == [
        AUTOSOMES,
        CHR_X,
        CHR_Y,
    ]
    assert "below the 15x that diploid" in warnings[0]
    assert "below the 3x that haploid" in warnings[1]
    assert "Mean depth from the BAM index" in caplog.text


@needs_htslib
def test_cli_override_and_zero_disable(sample, caplog):
    assert warn_low_depth(sample["bam"], {AUTOSOMES}, min_depth=2) is not None
    assert warn_low_depth(sample["bam"], {AUTOSOMES}, min_depth=0) is None
    assert not _warnings(caplog)


@needs_htslib
def test_cram_depth_comes_from_container_headers(sample, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cram = tmp_path / "s.cram"
    run_command(
        ["samtools", "view", "-C", "-T", sample["ref"], "--output-fmt-option",
         "multi_seq_per_slice=0", "-o", str(cram), sample["bam"]]
    )  # fmt: skip
    # No index, no decoding: idxstats would need one, so it can't have been used.
    with patch("wgsextract_cli.commands.info.metrics.parse_idxstats") as idxstats:
        depths = warn_low_depth(str(cram), {MTDNA}, ref=sample["ref"])
    idxstats.assert_not_called()
    assert depths == pytest.approx(
        {AUTOSOMES: 2.5, CHR_X: 1.25, CHR_Y: 2.0, MTDNA: 20.0}
    )
    assert "CRAM container headers" in caplog.text
    assert not _warnings(caplog)
    # htslib packs the small chrY and chrM into a multi-reference container,
    # whose header doesn't say which contigs hold its reads; its index does.
    run_command(
        ["samtools", "view", "-C", "-T", sample["ref"], "-o", str(cram), sample["bam"]]
    )
    assert warn_low_depth(str(cram), {AUTOSOMES}, ref=sample["ref"]) is None
    run_command(["samtools", "index", str(cram)])
    depths = warn_low_depth(str(cram), {AUTOSOMES}, ref=sample["ref"])
    assert depths == pytest.approx({AUTOSOMES: 2.5, CHR_X: 1.25}, rel=0.1)
    assert warn_low_depth(str(cram), {MTDNA}, ref=sample["ref"]) is None
    assert "isn't known without reading it" in caplog.text


@needs_htslib
def test_info_cache_is_used_before_counting(sample, tmp_path):
    bam = tmp_path / "c.bam"
    bam.write_bytes(Path(sample["bam"]).read_bytes())
    Path(f"{bam}.bai").write_bytes(Path(f"{sample['bam']}.bai").read_bytes())
    argv = ["wgsextract-cli", "info", "--detailed", "--input", str(bam)]
    argv += ["--ref", sample["ref"], "--outdir", str(tmp_path)]
    with patch.object(sys, "argv", argv):
        main()
    with patch.object(genotype_qc, "_counted_depths", side_effect=AssertionError):
        depths = warn_low_depth(str(bam), {AUTOSOMES}, outdir=str(tmp_path))
    assert depths is not None and depths[AUTOSOMES] == pytest.approx(2.5, rel=0.1)


def test_unknown_depth_and_vcf_inputs_never_warn(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    vcf = tmp_path / "s.vcf.gz"
    vcf.touch()
    assert warn_low_depth(str(vcf), {AUTOSOMES}) is None
    unindexed = tmp_path / "s.bam"
    unindexed.write_bytes(b"not read")
    assert warn_low_depth(str(unindexed), {AUTOSOMES}) is None
    assert "isn't known without reading it" in caplog.text
    assert not _warnings(caplog)


def _kit(path: Path, hom: int, het: int, no_calls: int = 0) -> Path:
    rows = ["# rsid\tchromosome\tposition\tgenotype"]
    rows += [f"rs{i}\t{1 + i % 22}\t{i}\tAA" for i in range(hom)]
    rows += [f"rs{i}\t2\t{i}\tAG" for i in range(het)]
    rows += [f"rs{i}\t3\t{i}\t--" for i in range(no_calls)]
    rows += [f"rsx{i}\tX\t{i}\tAG" for i in range(500)]  # not autosomal
    path.write_text("\n".join(rows) + "\n")
    return path


@pytest.mark.parametrize(
    ("hom", "het", "expected"),
    [
        (780, 220, None),
        (890, 110, "89.0% of autosomal genotypes are homozygous"),
        (960, 40, "96.0% of autosomal genotypes are homozygous"),
        (500, 500, "Only 50.0% of autosomal genotypes"),
        (95, 5, None),  # too few sites to judge
    ],
)
def test_homozygosity_warnings(tmp_path, caplog, hom, het, expected):
    caplog.set_level(logging.INFO)
    tally = warn_about_genotypes(_kit(tmp_path / "kit.txt", hom, het, 300))
    assert (tally.called, tally.het, tally.sites) == (hom + het, het, hom + het + 300)
    warnings = _warnings(caplog)
    assert warnings[0].startswith(expected) if expected else not warnings
    assert f"het/hom {het / hom:.2f}" in caplog.text


def test_no_call_rates_are_reported_together(tmp_path, caplog):
    kit = _kit(tmp_path / "kit.txt", 780, 220)
    outputs = {
        "23andMe_V5": GenotypeTally(sites=2000, called=1900, het=0),
        "Ancestry_V2": GenotypeTally(sites=2000, called=1500, het=0),
        "FTDNA_V3": GenotypeTally(sites=100, called=10, het=0),
    }
    warn_about_genotypes(kit, outputs)
    [warning] = _warnings(caplog)
    assert "qc_max_no_call_rate 10%): Ancestry_V2 25.0%." in warning
    caplog.clear()
    # Region runs leave most chip sites uncalled by design.
    warn_about_genotypes(kit, outputs, check_no_calls=False)
    assert not _warnings(caplog)


def test_thresholds_come_from_config(tmp_path, caplog):
    kit = _kit(tmp_path / "kit.txt", 960, 40)
    with patch.dict(genotype_qc.settings, {"qc_max_autosomal_homozygosity": 0.97}):
        warn_about_genotypes(kit)
    assert not _warnings(caplog)


def test_chrY_only_cram_keeps_the_assembly_n_adjustment(tmp_path):
    # A chrY extract keeps the full GRCh38 header; chrY is 57.2 Mb with
    # 33.6 Mb of N, so 10x over its non-N bases is 4.1x over its length.
    header = "@SQ\tSN:chr1\tLN:248956422\n@SQ\tSN:chrY\tLN:57227415\n"
    cram = tmp_path / "y.cram"
    cram.touch()
    with (
        patch.object(
            genotype_qc,
            "cram_container_totals",
            return_value={1: (1_575_757, 236_363_550)},
        ),
        patch(
            "wgsextract_cli.core.alignment_metadata.get_bam_header",
            return_value=header,
        ),
    ):
        depths = warn_low_depth(str(cram), {CHR_Y})
    assert depths == pytest.approx(
        {AUTOSOMES: 0.0, CHR_X: 0.0, CHR_Y: 10.0, MTDNA: 0.0}
    )


@pytest.fixture(scope="module")
def slice_bam(tmp_path_factory):
    """A 2 Mb chr20 whose reads (20x of 100 bp) cover only 20 kb from 1.2 Mb,
    as `extract` or a region-limited realign writes."""
    root = tmp_path_factory.mktemp("slice")
    rng = random.Random(5)
    seq = "".join(rng.choice("ACGT") for _ in range(2_000_000))
    ref = write_fasta(root / "ref.fa", {"chr20": seq})
    reads = [
        f"r{i}\t0\tchr20\t{pos}\t60\t100M\t*\t0\t0\t{seq[pos - 1 : pos + 99]}\t"
        + "I" * 100
        for i, pos in enumerate(range(1_200_001, 1_219_902, 5))
    ]
    bam = sam_to_bam(["@SQ\tSN:chr20\tLN:2000000", *reads], root / "slice.bam")
    return {"root": root, "ref": str(ref), "bam": bam}


@needs_htslib
def test_issue_430_slice_isnt_called_low_coverage_over_the_whole_contig(
    slice_bam, caplog
):
    # #430: a 20x slice warned "mean autosomes depth of about 0.2x".
    depths = warn_low_depth(slice_bam["bam"], {AUTOSOMES}, ref=slice_bam["ref"])
    assert depths is not None and depths[AUTOSOMES] == pytest.approx(20, rel=0.05)
    assert not _warnings(caplog)
    assert warn_low_depth(slice_bam["bam"], {AUTOSOMES}, min_depth=25)
    assert "about 20.0x, below the 25x" in _warnings(caplog)[0]


@needs_htslib
@pytest.mark.parametrize(
    ("region", "warned"),
    [
        ("chr20:1200001-1220000", False),
        ("chr20:1500000-1600000", True),
        ("chr20:1100001-1300000", True),
    ],
)
def test_issue_430_vcf_region_is_judged_by_its_own_depth(
    slice_bam, tmp_path, region, warned
):
    # #430: `vcf ... --region` warned about the whole genome's depth.
    outdir = tmp_path / "out"
    argv = ["vcf", "snp", "--input", slice_bam["bam"], "--ref", slice_bam["ref"]]
    argv += ["--region", region, "--ploidy", "2", "--outdir", str(outdir)]
    main([*argv, "--config", str(tmp_path / "config.toml")])
    (manifest,) = outdir.glob("*.manifest.json")
    warnings = json.loads(manifest.read_text())["warnings"]
    low = [w for w in warnings if w["code"] == "low_coverage"]
    assert bool(low) is warned
    if warned:
        assert low[0]["details"]["depth"] < 3


@needs_htslib
def test_issue_430_region_names_like_the_header_or_not(slice_bam, caplog):
    # `20:...` against a chr20 header, with thousands separators, is the slice.
    for region in ("20:1,200,001-1,220,000", "chr20:1200001-1220000 chrUn"):
        depths = warn_low_depth(
            slice_bam["bam"], {AUTOSOMES}, ref=slice_bam["ref"], region=region
        )
        assert depths is not None
        assert depths[AUTOSOMES] == pytest.approx(20, rel=0.05)
    assert not _warnings(caplog)


@needs_htslib
def test_issue_430_region_without_chrY_reads_keeps_a_man_haploid(tmp_path, caplog):
    # A man's 8x chrX is haploid even when the region's chrY has no reads.
    rng = random.Random(7)
    seqs = {c: "".join(rng.choice("ACGT") for _ in range(200_000)) for c in "XY"}
    ref = write_fasta(tmp_path / "ref.fa", {f"chr{c}": s for c, s in seqs.items()})
    reads = [
        f"{c}{pos}\t0\tchr{c}\t{pos}\t60\t100M\t*\t0\t0\t"
        f"{seqs[c][pos - 1 : pos + 99]}\t" + "I" * 100
        for c, last in (("X", 199_901), ("Y", 99_901))
        for pos in range(1, last + 1, 12)
    ]
    header = ["@SQ\tSN:chrX\tLN:200000", "@SQ\tSN:chrY\tLN:200000"]
    bam = sam_to_bam([*header, *reads], tmp_path / "male.bam")
    region = "chrX,chrY:150001-200000"
    warn_low_depth(bam, {CHR_X, CHR_Y}, ref=str(ref), region=region)
    assert [w.split(" depth")[0] for w in _warnings(caplog)] == [
        "Low coverage: male.bam has a mean chrY"
    ]
