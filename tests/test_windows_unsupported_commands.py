import logging
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from wgsextract_cli.cli import dispatch
from wgsextract_cli.cli.parser import build_parser, parse_args

UNSUPPORTED = [
    (["vcf", "phylop"], "pyBigWig"),
    (["vcf", "gerp"], "pyBigWig"),
    (["ref", "phylop"], "pyBigWig"),
    (["ref", "gerp"], "pyBigWig"),
    (["vcf", "cnv"], "delly"),
    (["vcf", "sv"], "delly"),
    (["vcf", "sv", "--caller", "pbsv"], "pbsv"),
    (["vcf", "sv", "--caller", "sniffles"], "sniffles"),
    (["vcf", "sv", "--pacbio"], "pbsv/sniffles"),
    (["vcf", "freebayes"], "FreeBayes"),
    (["vcf", "deepvariant"], "DeepVariant"),
    (["vcf", "pharmcat"], "PharmCAT"),
    (["vep"], "VEP"),
    (["vep", "run"], "VEP"),
    (["vep", "download"], "VEP"),
    (["vep", "verify"], "VEP"),
    (["typing", "cyp2d6"], "Cyrius"),
    (["typing", "hla"], "T1K"),
    (["typing", "str"], "ExpansionHunter"),
    (["align", "--aligner", "pbmm2"], "pbmm2"),
]


@pytest.mark.parametrize(("command", "tool"), UNSUPPORTED)
def test_unsupported_windows_command_fails_before_work(
    command: list[str],
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    parser, base = build_parser()
    outdir = tmp_path / "must-not-exist"
    args, explicit = parse_args(
        parser, base, [*command, "--input", "missing.bam", "--outdir", str(outdir)]
    )
    handler = Mock(side_effect=AssertionError("command must not start"))
    selection = Mock(side_effect=AssertionError("inputs must not be resolved"))
    args.func = handler
    monkeypatch.setattr(dispatch, "apply_genome_selection", selection)
    monkeypatch.setattr(sys, "platform", "win32")
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
        dispatch.dispatch_command(args, explicit)
    assert exc.value.code == 1
    assert f"{tool} is not supported on Windows" in caplog.text
    assert "unexpected error" not in caplog.text.lower()
    assert not outdir.exists()
    handler.assert_not_called()
    selection.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_windows_restrictions_do_not_change_unix_dispatch(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    parser, base = build_parser()
    args, explicit = parse_args(parser, base, ["vcf", "phylop"])
    args.func = Mock()
    monkeypatch.setattr(dispatch, "apply_genome_selection", Mock())
    monkeypatch.setattr(sys, "platform", platform)
    dispatch.dispatch_command(args, explicit)
    args.func.assert_called_once_with(args)
