import argparse
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.commands import _deps_status, align, realign
from wgsextract_cli.commands.vcf import callers, deepvariant
from wgsextract_cli.core import (
    dependencies,
    dependency_checks,
    native_support,
    parabricks,
)
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def linux_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")


@pytest.mark.parametrize("tool", ["gatk", "run_deepvariant"])
def test_arm_dependency_gate_precedes_resolution(
    linux_arm: None, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    probe = Mock(side_effect=AssertionError("must not probe or install tools"))
    monkeypatch.setattr(dependency_checks, "check_dependencies", probe)
    monkeypatch.setattr(dependency_checks, "get_tool_path", probe)
    with pytest.raises(WGSExtractError, match="phase 1"):
        dependency_checks.verify_dependencies([tool])
    version = dependency_checks.get_tool_version(tool)
    assert version is not None and "phase 1" in version
    probe.assert_not_called()


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("command", [callers.cmd_gatk, deepvariant.cmd_deepvariant])
def test_arm_callers_fail_before_input_or_container_work(
    linux_arm: None,
    monkeypatch: pytest.MonkeyPatch,
    gpu: bool,
    command: Callable[[argparse.Namespace], None],
) -> None:
    probe = Mock(side_effect=AssertionError("must fail before any execution"))
    monkeypatch.setattr(callers, "get_base_args", probe)
    monkeypatch.setattr(deepvariant, "get_tool_path", probe)
    monkeypatch.setattr(deepvariant, "_deepvariant_gpu", probe)
    monkeypatch.setattr(callers, "_gatk_gpu", probe)
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        command(argparse.Namespace(gpu=gpu))
    probe.assert_not_called()


@pytest.mark.parametrize("subcommand", ["gatk", "deepvariant"])
def test_arm_cli_gate_needs_no_input(linux_arm: None, subcommand: str) -> None:
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        verify_platform_support(argparse.Namespace(command="vcf", vcf_cmd=subcommand))


@pytest.mark.parametrize("tool", ["gatk", "run_deepvariant"])
def test_arm_selected_dependency_gate_precedes_resolution(
    linux_arm: None, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    probe = Mock(side_effect=AssertionError("must not resolve unsupported tools"))
    monkeypatch.setattr(dependencies, "get_tool_path", probe)
    args = argparse.Namespace(
        command="deps", subcommand="check", tool=tool, debug=False
    )
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        verify_platform_support(args)
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        _deps_status.run(args)
    probe.assert_not_called()


@pytest.mark.parametrize("tool", ["gatk", "run_deepvariant"])
def test_arm_missing_optional_tool_explains_restriction(
    linux_arm: None, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    monkeypatch.setattr(dependency_checks, "get_tool_path", lambda _: None)
    install = Mock(
        side_effect=AssertionError("must not suggest installing a rejected tool")
    )
    monkeypatch.setattr(dependency_checks, "pixi_install_command", install)
    (row,) = dependency_checks.check_all_dependencies([], [tool])["optional"]
    assert row["path"] is None
    assert "Linux ARM64" in row["version"]
    assert "pixi install" not in row["version"]
    install.assert_not_called()


@pytest.mark.parametrize("command", ["align", "realign"])
def test_arm_explicit_ultima_gate_needs_no_input(linux_arm: None, command: str) -> None:
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        verify_platform_support(
            argparse.Namespace(command=command, platform="ultima", gpu=False)
        )


@pytest.mark.parametrize("command", ["align", "realign"])
def test_arm_autodetected_ultima_fails_before_preparation(
    linux_arm: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    bam = tmp_path / "ultima.bam"
    bam.write_bytes(b"header supplied by mock")
    monkeypatch.setattr(align, "get_bam_header", lambda _: "@RG\tID:u\tPL:ULTIMA\n")
    preparation = Mock(
        side_effect=AssertionError("must not prepare an Ultima alignment")
    )
    monkeypatch.setattr(align, "_bwa_setup", preparation)
    monkeypatch.setattr(realign, "resolve_reference", preparation)
    args = argparse.Namespace(
        platform=None,
        aligner="auto",
        long_read=False,
        preset=None,
        gpu=False,
        r1=str(bam) if command == "align" else None,
        r2=None,
        input=str(bam),
    )
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        (align.run if command == "align" else realign.run)(args)
    preparation.assert_not_called()


def test_arm_ultima_direct_entry_points_do_not_probe_or_execute(
    linux_arm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Mock(side_effect=AssertionError("must not probe or execute GATK"))
    monkeypatch.setattr(align, "_bwa_setup", probe)
    monkeypatch.setattr(align, "get_tool_path", probe)
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        align.align_ultima(argparse.Namespace(), "absent.bam")
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        align._sort_and_mark_ultima(
            [],
            "absent.bam",
            align._AlignSetup("16", "256M", "absent", "absent.fa", "s"),
            "BAM",
            "absent",
        )
    probe.assert_not_called()


@pytest.mark.parametrize("command", ["align", "realign"])
def test_arm_gpu_alignment_gate_needs_no_input(linux_arm: None, command: str) -> None:
    with pytest.raises(WGSExtractError, match="Parabricks --gpu"):
        verify_platform_support(argparse.Namespace(command=command, gpu=True))
    verify_platform_support(argparse.Namespace(command=command, gpu=False))


def test_arm_parabricks_gate_precedes_setup_or_launch(
    linux_arm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Mock(side_effect=AssertionError("must not inspect or launch Docker"))
    monkeypatch.setattr(parabricks, "parabricks_image", probe)
    monkeypatch.setattr(parabricks, "run_container", probe)
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        parabricks.verify_parabricks()
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        parabricks.run_pbrun("fq2bam", [], [])
    with pytest.raises(WGSExtractError, match="Linux ARM64"):
        parabricks.call_variants("deepvariant", "absent", "absent", "absent/file", None)
    probe.assert_not_called()


@pytest.mark.parametrize("architecture", ["x86_64", "AMD64"])
def test_x64_policy_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, architecture: str
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: architecture)
    for tool in ("gatk", "run_deepvariant", "parabricks"):
        native_support.require_native_tool(tool)


def test_macos_arm_policy_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    for tool in ("gatk", "run_deepvariant", "parabricks"):
        native_support.require_native_tool(tool)
