"""Y extractions of a female sample say they hold no Y-DNA."""

import logging
from unittest.mock import patch

import pytest

from wgsextract_cli.commands import extract
from wgsextract_cli.commands.info import metrics


@pytest.mark.parametrize(("sex", "warns"), [("Female", True), ("Male", False)])
def test_y_extraction_of_a_female_sample_warns(caplog, sex, warns):
    with (
        patch.object(metrics, "sample_sex", return_value=sex),
        caplog.at_level(logging.WARNING),
    ):
        extract._warn_if_female("sample.cram", ".")
    assert ("holds no Y-DNA" in caplog.text) == warns
