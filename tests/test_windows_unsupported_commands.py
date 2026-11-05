import logging
import sys
from pathlib import Path

import pytest

from wgsextract_cli.cli import dispatch
from wgsextract_cli.cli.parser import build_parser, parse_args
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.core.config import settings

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
    (["realign", "--aligner", "pbmm2"], "pbmm2"),
]


@pytest.fixture
def genome(tmp_path: Path) -> Path:
    """A genome folder in the library: `--genome joe` writes its
    genome-config.toml and makes it the output folder once resolved."""
    folder = tmp_path / "library" / "joe"
    folder.mkdir(parents=True)
    settings["genome_library"] = str(folder.parent)
    return folder


def _dispatch(
    argv: list[str], platform: str, monkeypatch: pytest.MonkeyPatch
) -> int | str | None:
    """Run a command line through dispatch as `platform`; its exit code."""
    args, explicit = parse_args(*build_parser(), argv)
    monkeypatch.setattr(sys, "platform", platform)
    try:
        dispatch.dispatch_command(args, explicit)
    except SystemExit as e:
        return e.code
    return 0


@pytest.mark.parametrize(("command", "tool"), UNSUPPORTED)
def test_unsupported_windows_command_fails_before_work(
    command: list[str],
    tool: str,
    genome: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outdir = tmp_path / "must-not-exist"
    argv = [*command, "--genome", "joe", "--input", "missing.bam",
            "--outdir", str(outdir)]  # fmt: skip
    with caplog.at_level(logging.ERROR):
        code = _dispatch(argv, "win32", monkeypatch)
    # Only an option choice (--aligner pbmm2) is a usage error; whole commands fail.
    assert code == (2 if "--aligner" in command else 1)
    assert f"{tool} is not supported on Windows" in caplog.text
    assert "unexpected error" not in caplog.text.lower()
    assert not outdir.exists()
    assert list(genome.iterdir()) == []


@pytest.mark.parametrize(
    "command",
    [
        ["align", "--gpu"],
        ["realign", "--gpu"],
        ["vcf", "gatk", "--gpu"],
        ["vcf", "deepvariant", "--gpu"],
    ],
)
@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_gpu_modes_fail_before_work_on_non_linux(
    command: list[str],
    platform: str,
    genome: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        code = _dispatch([*command, "--genome", "joe"], platform, monkeypatch)
    assert code == 2
    assert "Parabricks --gpu is not supported" in caplog.text
    assert list(genome.iterdir()) == []


def _passes_platform_check(
    argv: list[str], platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command line gets past the platform check: dispatch refuses
    nothing for it. Running the command itself would need the platform's tools,
    which a faked sys.platform can't find."""
    args, _ = parse_args(*build_parser(), argv)
    monkeypatch.setattr(sys, "platform", platform)
    verify_platform_support(args)


@pytest.mark.parametrize("extension", [".tsv.gz", ".vcf.gz"])
@pytest.mark.parametrize("source", ["explicit", "config", "library"])
def test_windows_phylop_tables_remain_supported(
    extension: str,
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "hg38"
    library.mkdir()
    resource = library / f"phylop_hg38{extension}"
    resource.touch()
    settings["reference_library"] = str(library)
    argv = ["vcf", "phylop"]
    if source == "explicit":
        argv += ["--phylop-file", str(resource)]
    elif source == "config":
        settings["phylop_tsv_path"] = str(resource)
    _passes_platform_check(argv, "win32", monkeypatch)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_windows_restrictions_do_not_change_unix_dispatch(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _passes_platform_check(["vcf", "phylop"], platform, monkeypatch)
