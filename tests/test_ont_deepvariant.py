from pathlib import Path

import pytest

from wgsextract_cli.api import VcfDeepvariantParams
from wgsextract_cli.api._params import from_namespace
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.commands.vcf import deepvariant
from wgsextract_cli.core.utils import WGSExtractError


def _params(**values) -> VcfDeepvariantParams:
    return VcfDeepvariantParams(**{"input": Path("reads.bam"), **values})


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ({}, "ONT_R104"),
        ({"platform": "illumina"}, "WGS"),
        ({"platform": "pacbio"}, "WGS"),
        ({"platform": None}, "WGS"),
        ({"model_type": "WGS"}, "WGS"),
        ({"model_type": "WES"}, "WES"),
        ({"pacbio": True}, "PACBIO"),
        ({"wes": True}, "WES"),
        ({"platform": None, "ont": True}, "ONT_R104"),
        ({"platform": None, "model_type": "ONT_R104"}, "ONT_R104"),
    ],
)
def test_ont_model_default_and_explicit_overrides(selection, expected):
    selection = {"platform": "ont", **selection}
    platform = selection.pop("platform")
    params = _params(**selection)
    assert deepvariant._model_type(params, platform=platform) == expected


def test_model_uses_shared_resolver_with_decoding_reference(monkeypatch, caplog):
    calls = []

    def detect(alignment, ref):
        calls.append((alignment, ref))
        return "ont"

    monkeypatch.setattr(deepvariant, "input_platform", detect)
    with caplog.at_level("INFO"):
        assert (
            deepvariant._model_type(_params(input=Path("reads.cram")), "ref.fa")
            == "ONT_R104"
        )
    assert calls == [("reads.cram", "ref.fa")]
    assert "R10.4.1 only" in caplog.text


@pytest.mark.parametrize(
    "selection",
    [
        {"ont": True, "pacbio": True},
        {"ont": True, "wes": True},
        {"ont": True, "model_type": "WGS"},
        {"model_type": "ONT_R104", "pacbio": True},
        {"model_type": "ONT_R104", "wes": True},
    ],
)
def test_contradictory_ont_selectors_are_refused(selection):
    with pytest.raises(WGSExtractError, match="cannot be combined"):
        deepvariant._model_type(_params(**selection))


@pytest.mark.parametrize(
    ("header", "warning"),
    [
        ("@PG\tID:dorado\tCL:dorado dna_r9.4.1_450bps_sup@v3.3\n", True),
        ("@RG\tID:run\tDS:basecall_model=dna_r9_4_1_hac\n", True),
        ("@RG\tID:run\tDS:chemistry=R9.4\n", True),
        ("@PG\tID:dorado\tCL:dna_r10.4.1_e8.2_400bps_sup\n", False),
        ("@RG\tID:run\tSM:r9.4.1\tPL:ONT\n", False),
        ("@CO\tr9.4.1 is not basecaller metadata\n", False),
        ("", False),
    ],
)
def test_r9_warning_uses_basecaller_metadata_only(monkeypatch, caplog, header, warning):
    monkeypatch.setattr(deepvariant, "get_bam_header", lambda *_: header)
    deepvariant._warn_ont_chemistry("reads.bam", "ref.fa")
    assert ("chemistry is unsupported" in caplog.text) is warning


@pytest.mark.parametrize("selection", [["--ont"], ["--model-type", "ONT_R104"]])
def test_cli_accepts_ont_selectors(selection):
    parser, _ = build_parser()
    args = parser.parse_args(["vcf", "deepvariant", "-i", "reads.bam", *selection])
    params = from_namespace(VcfDeepvariantParams, args)
    assert deepvariant._model_type(params) == "ONT_R104"
