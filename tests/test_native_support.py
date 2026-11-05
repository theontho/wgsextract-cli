import argparse
import os
import platform
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fake_tools import FakePixi, python_tool, require_bare_name_fakes
from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.core import dependency_checks, native_support, parabricks
from wgsextract_cli.core.runtime import source_checkout_root
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.main import main

_RECORD = "import sys\nopen({log!r}, 'a').write({tool!r} + '\\n')\n"


@pytest.fixture
def arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Linux ARM64 with programs it mustn't start on PATH: x86-only GATK, and
    Parabricks' Docker and NVIDIA driver; GATK's pixi environment isn't
    installed yet. ``launched()`` lists what was started."""
    require_bare_name_fakes()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    root = source_checkout_root()
    pixi = FakePixi(tmp_path / "pixi", root / "pixi.toml" if root else None)
    log = tmp_path / "launched.txt"
    for tool in ("gatk", "docker", "nvidia-smi"):
        python_tool(pixi.host_bin, tool, _RECORD.format(log=str(log), tool=tool))
    gatk_env = pixi.env("gatk", locked=("gatk4",), installed=False)
    python_tool(gatk_env / "bin", "gatk", _RECORD.format(log=str(log), tool="gatk"))
    monkeypatch.setenv("PIXI_EXE", str(pixi.exe))
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)
    # The host's own programs (samtools) stay reachable behind them.
    monkeypatch.setenv("PATH", f"{pixi.host_bin}{os.pathsep}{os.environ['PATH']}")
    return SimpleNamespace(
        launched=lambda: log.read_text().split() if log.exists() else [],
        gatk_installed=lambda: pixi.installed("gatk"),
    )


def _nothing_started(arm: SimpleNamespace) -> None:
    assert arm.launched() == []
    assert not arm.gatk_installed()


def _refusal(
    runner: str, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int | None, str]:
    """Run argv through the CLI (exit code, stderr), or straight into the
    command as an API caller does, past the CLI's own checks."""
    if runner == "command":
        with pytest.raises(WGSExtractError) as error:
            run_wgsextract(*argv)
        return None, str(error.value)
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    return exit_.value.code, capsys.readouterr().err


_ALIGN = ["--r1", "reads.fq", "--ref", "ref.fa"]
_REALIGN = ["--input", "reads.bam", "--ref", "ref.fa"]
_CALL = ["--input", "reads.bam", "--ref", "ref.fa"]


_BOTH = ("cli", "command")
_REFUSALS = [
    (["vcf", "gatk", *_CALL], 1, "GATK is not supported on Linux ARM64", _BOTH),
    (["vcf", "gatk", "--gpu", *_CALL], 1, "GATK is not supported", _BOTH),
    # CPU DeepVariant runs on ARM64; only the --gpu option is refused.
    (["vcf", "deepvariant", "--gpu", *_CALL], 2, "Parabricks --gpu is", _BOTH),
    (["deps", "check", "--tool", "gatk"], 1, "GATK is not supported", _BOTH),
    (["align", "--platform", "ultima", *_ALIGN], 1, "GATK is not", _BOTH),
    # The commands look for these inputs first; the CLI refuses before that.
    (["align", "--gpu", *_ALIGN], 2, "Parabricks --gpu is not", ("cli",)),
    (["realign", "--gpu", *_REALIGN], 2, "Parabricks --gpu is not", ("cli",)),
    (["realign", "--platform", "ultima", *_REALIGN], 1, "GATK is not", ("cli",)),
]


@pytest.mark.parametrize(
    ("runner", "argv", "code", "message"),
    [
        pytest.param(runner, argv, code, message, id=f"{runner}-{' '.join(argv[:3])}")
        for argv, code, message, runners in _REFUSALS
        for runner in runners
    ],
)
def test_arm_refuses_gatk_and_gpu_work_before_starting_anything(
    arm, tmp_path, monkeypatch, capsys, runner, argv, code, message
):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"

    exit_code, error = _refusal(runner, [*argv, *_outdir(argv, out)], capsys)

    assert message in error
    if runner == "cli":
        assert exit_code == code
    _nothing_started(arm)
    assert not out.exists()


def _outdir(argv: list[str], out: Path) -> list[str]:
    return [] if argv[0] == "deps" else ["--outdir", str(out)]


@needs_tools("samtools")
@pytest.mark.parametrize("runner", ["cli", "command"])
@pytest.mark.parametrize("command", ["align", "realign"])
def test_arm_refuses_reads_found_to_be_ultima_before_preparing_them(
    arm, tmp_path, capsys, runner, command
):
    """Ultima needs GATK's flow-mode duplicate marking: the read group tells."""
    seq = "ACGT" * 25
    bam = sam_to_bam(
        [
            "@RG\tID:u\tSM:s\tPL:ULTIMA",
            f"r\t4\t*\t0\t0\t*\t*\t0\t0\t{seq}\t{'I' * 100}\tRG:Z:u",
        ],
        tmp_path / "ultima.bam",
    )
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\n" + "ACGT" * 100 + "\n")
    out = tmp_path / "out"
    reads = ["--r1", bam] if command == "align" else ["--input", bam]
    argv = [command, *reads, "--ref", str(ref), "--outdir", str(out)]

    _, error = _refusal(runner, argv, capsys)

    assert "GATK is not supported on Linux ARM64" in error
    _nothing_started(arm)
    assert not out.exists() or not any(out.iterdir())
    # Nothing prepared beside the reference either (no index).
    assert sorted(p.name for p in tmp_path.glob("ref.*")) == ["ref.fa"]


def test_arm_gatk_dependency_check_names_the_restriction(arm):
    with pytest.raises(WGSExtractError, match="phase 1"):
        dependency_checks.verify_dependencies(["gatk"])
    version = dependency_checks.get_tool_version("gatk")
    assert version is not None and "phase 1" in version
    (row,) = dependency_checks.check_all_dependencies([], ["gatk"])["optional"]
    assert row["path"] is None
    assert "Linux ARM64" in row["version"]
    # A tool that can't run here isn't offered for installing.
    assert "pixi install" not in row["version"]
    _nothing_started(arm)


def test_arm_parabricks_entry_points_refuse_without_docker(arm, tmp_path):
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        parabricks.verify_parabricks()
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        parabricks.run_pbrun("fq2bam", [], [])
    out = tmp_path / "out" / "calls.vcf.gz"
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        parabricks.call_variants("deepvariant", "in.bam", "ref.fa", str(out), None)
    _nothing_started(arm)
    assert not out.parent.exists()


def test_arm_cpu_deepvariant_passes_platform_gate(arm) -> None:
    native_support.require_native_tool("run_deepvariant")
    verify_platform_support(
        argparse.Namespace(command="vcf", vcf_cmd="deepvariant", gpu=False)
    )
    for command in ("align", "realign"):
        verify_platform_support(argparse.Namespace(command=command, gpu=False))


@pytest.mark.parametrize(
    ("system", "architecture"),
    [("linux", "x86_64"), ("linux", "AMD64"), ("darwin", "arm64")],
)
def test_other_platforms_keep_gatk_and_parabricks(
    monkeypatch: pytest.MonkeyPatch, system: str, architecture: str
) -> None:
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(platform, "machine", lambda: architecture)
    for tool in ("gatk", "run_deepvariant", "parabricks"):
        native_support.require_native_tool(tool)
