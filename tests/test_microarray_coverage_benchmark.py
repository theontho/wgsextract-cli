import pytest

from scripts.benchmark_microarray_coverage import fraction_digits, score
from wgsextract_cli.commands.benchmark.chip import ChipCall


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
