from types import SimpleNamespace

import pytest

from wgsextract_cli.core import microarray_quality as quality
from wgsextract_cli.core.utils import WGSExtractError


def test_config_policy_and_explicit_cli_overrides(monkeypatch):
    monkeypatch.setattr(
        quality,
        "settings",
        {
            "microarray_min_gq": 25,
            "microarray_min_dp": 5,
            "microarray_assume_hom_ref": False,
            "microarray_call_reasons": True,
        },
    )
    policy = quality.quality_policy(SimpleNamespace(min_gq=10, assume_hom_ref=True))
    assert policy.min_gq == 10
    assert policy.min_dp == 5
    assert policy.assume_hom_ref is True
    assert policy.call_reasons is True
    assert policy.rejection("5", "10", "PASS") is None
    assert policy.rejection("4", "10", "PASS") == "low_dp"
    assert policy.rejection("5", ".", "PASS") == "missing_gq"


@pytest.mark.parametrize("value", [-1, True, 2.5, "10"])
def test_invalid_quality_configuration_fails_cleanly(monkeypatch, value):
    monkeypatch.setattr(quality, "settings", {"microarray_min_dp": value})
    with pytest.raises(WGSExtractError, match="non-negative integer"):
        quality.quality_policy(SimpleNamespace())


def test_filtered_and_nonfinite_quality_are_explicit_no_calls():
    policy = quality.MicroarrayQuality(min_gq=10, exclude_filtered=True)
    assert policy.rejection("10", "25", "q10") == "filtered"
    assert policy.rejection("10", "inf", "PASS") == "missing_gq"
    assert quality.MicroarrayQuality().rejection(".", ".", "q10") is None


def test_quality_does_not_silently_select_a_sample(monkeypatch, tmp_path):
    from wgsextract_cli.commands import _microarray_combined

    monkeypatch.setattr(
        _microarray_combined, "_command_lines", lambda *_args: iter(["S1\n", "S2\n"])
    )
    with pytest.raises(WGSExtractError, match="single-sample"):
        _microarray_combined._validate_quality_sample(
            quality.MicroarrayQuality(min_gq=10), "input.vcf.gz", str(tmp_path)
        )
