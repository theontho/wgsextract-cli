import argparse
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.commands.vcf import callers, deepvariant
from wgsextract_cli.core import dependency_checks, native_support
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def linux_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_support.sys, "platform", "linux")
    monkeypatch.setattr(native_support.platform, "machine", lambda: "aarch64")


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


@pytest.mark.parametrize("architecture", ["x86_64", "AMD64"])
def test_x64_policy_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, architecture: str
) -> None:
    monkeypatch.setattr(native_support.sys, "platform", "linux")
    monkeypatch.setattr(native_support.platform, "machine", lambda: architecture)
    for tool in ("gatk", "run_deepvariant"):
        native_support.require_native_tool(tool)


def test_macos_arm_policy_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_support.sys, "platform", "darwin")
    monkeypatch.setattr(native_support.platform, "machine", lambda: "arm64")
    for tool in ("gatk", "run_deepvariant"):
        native_support.require_native_tool(tool)
