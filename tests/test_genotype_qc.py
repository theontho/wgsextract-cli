"""Low-coverage and imputation warnings: depth from the info cache, CRAM
container headers or a BAM index, and genotype rates from written kits."""

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
            pos = 1 + (i * 13) % (LENGTHS[contig] - 50)
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
    assert region_targets("chrUn_KI270302v1") == set()


@needs_htslib
def test_bam_index_depth_warns_per_target_threshold(sample, caplog):
    caplog.set_level(logging.INFO)
    depths = warn_low_depth(sample["bam"], set(genotype_qc.TARGETS), ref=sample["ref"])
    assert depths == pytest.approx(
        {AUTOSOMES: 2.5, CHR_X: 1.25, CHR_Y: 2.0, MTDNA: 20.0}
    )
    warnings = _warnings(caplog)
    # Male (chrY > a quarter of chrX), so chrX is held to the haploid 3x.
    assert [w.split(" depth")[0].split()[-1] for w in warnings] == [
        AUTOSOMES,
        CHR_X,
        CHR_Y,
    ]
    assert "below the 8x that diploid" in warnings[0]
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
    # whose header doesn't say which contigs hold its reads.
    run_command(
        ["samtools", "view", "-C", "-T", sample["ref"], "-o", str(cram), sample["bam"]]
    )
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
    assert depths is not None and depths[AUTOSOMES] == pytest.approx(2.5)


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
