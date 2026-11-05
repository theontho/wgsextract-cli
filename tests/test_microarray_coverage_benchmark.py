from unittest.mock import patch

import pytest

from scripts.benchmark_microarray_coverage import (
    check_resume,
    current_warnings,
    fraction_digits,
    score,
)
from wgsextract_cli.commands.benchmark.chip import ChipCall
from wgsextract_cli.core import genotype_qc


def test_score_splits_low_coverage_errors_by_truth_class() -> None:
    truth = {
        "rs1": ChipCall("1", "AG"),
        "rs2": ChipCall("1", "AA"),
        "rs3": ChipCall("2", "CC"),
        "rs4": ChipCall("X", "TT"),
        "rs5": ChipCall("3", "AC"),
    }
    classes = {"rs1": "het", "rs2": "hom_ref", "rs3": "hom_alt", "rs4": "hom_alt"}
    classes["rs5"] = "het"
    kit = {
        "rs1": ChipCall("1", "AA"),  # het lost: one allele sampled
        "rs2": ChipCall("1", "AA"),
        "rs3": ChipCall("2", None),
        "rs4": ChipCall("X", "T"),  # haploid X matches a doubled truth
        "rs5": ChipCall("4", "AC"),  # chromosome mismatch: not eligible
        "rs9": ChipCall("5", "GT"),  # absent from truth: only kit rates
    }
    result = score(kit, truth, classes).as_dict()

    autosomes = result["groups"]["autosomes"]
    assert (autosomes["eligible"], autosomes["called"]) == (3, 2)
    assert (autosomes["concordant"], autosomes["het_called_hom"]) == (1, 1)
    assert result["groups"]["X"]["concordant"] == 1
    classes_out = result["classes"]["autosomes"]
    assert classes_out["het"]["accuracy"] == 0
    assert classes_out["hom_ref"]["accuracy"] == 1
    assert classes_out["hom_alt"]["call_rate"] == 0
    # The kit's own rates, as genotype QC sees them: 5 autosomal rows, 1 no-call.
    assert result["kit_autosomal_no_call_rate"] == pytest.approx(0.2)
    assert result["kit_autosomal_homozygosity"] == pytest.approx(0.5)


def test_fraction_is_written_as_samtools_seed_digits() -> None:
    assert fraction_digits(0.5) == "5"
    assert fraction_digits(0.033333) == "033333"
    for bad in (0.0, 1.0, 0.0000001):
        with pytest.raises(ValueError):
            fraction_digits(bad)


def test_resume_refuses_an_outdir_from_other_inputs(tmp_path) -> None:
    identity = {"input": ["a.cram", 1], "sample": "HG00096", "seed": 42}
    check_resume(tmp_path, identity)
    check_resume(tmp_path, identity)
    with pytest.raises(SystemExit, match="different sample, seed"):
        check_resume(tmp_path, {**identity, "sample": "HG00097", "seed": 7})


def test_cached_warnings_follow_the_current_depth_threshold() -> None:
    result = {
        "measured_depth": {"autosomes": 10.0, "chrX": 5.0, "chrY": 10.0},
        "warnings": ["high_homozygosity"],  # reported when the threshold was 8
    }
    with patch.dict(genotype_qc.settings, {"qc_min_diploid_depth": 15.0}):
        assert current_warnings(result) == [
            "high_homozygosity",
            "low_coverage:autosomes",
        ]
    result["warnings"].append("low_coverage:autosomes")
    with patch.dict(genotype_qc.settings, {"qc_min_diploid_depth": 8.0}):
        assert current_warnings(result) == ["high_homozygosity"]
