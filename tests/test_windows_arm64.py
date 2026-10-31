import argparse
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.core import dependency_checks, utils, windows_arm64
from wgsextract_cli.core.utils import WGSExtractError


@pytest.mark.parametrize("tool", ["gatk", "deepvariant"])
def test_arm64_callers_fail_before_input_or_tool_resolution(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    resolve = Mock(side_effect=AssertionError("must not resolve a tool"))
    monkeypatch.setattr(dependency_checks, "get_tool_path", resolve)
    monkeypatch.setattr(dependency_checks, "check_dependencies", resolve)
    with pytest.raises(WGSExtractError, match="native Windows ARM64"):
        verify_platform_support(argparse.Namespace(command="vcf", vcf_cmd=tool))
    with pytest.raises(WGSExtractError, match="native Windows ARM64"):
        dependency_checks.verify_dependencies([tool])
    assert "native Windows ARM64" in str(dependency_checks.get_tool_version(tool))
    resolve.assert_not_called()


def test_arm64_rejects_gatk_java_dispatch_before_process_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    with pytest.raises(WGSExtractError, match="pinned GKL"):
        utils._normalize_subprocess_cmd(
            [
                Path(sys.executable),
                "-cp",
                "gatk.jar",
                "org.broadinstitute.hellbender.Main",
            ]
        )


@pytest.mark.parametrize("machine", [0x8664, None])
def test_x64_windows_gatk_remains_available(
    machine: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: machine)
    verify_platform_support(argparse.Namespace(command="vcf", vcf_cmd="gatk"))
    assert windows_arm64.unsupported_windows_arm64_tool("gatk") is None


def test_os_architecture_is_queried_instead_of_emulated_python_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_arm64.platform, "machine", lambda: "AMD64")
    query = Mock()

    def set_native(_handle: object, _process: object, native: object) -> int:
        native._obj.value = 0xAA64  # type: ignore[attr-defined]
        return 1

    query.side_effect = set_native
    kernel = Mock(IsWow64Process2=query)
    monkeypatch.setattr(
        windows_arm64.ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False
    )
    assert windows_arm64.windows_native_machine() == 0xAA64
    assert windows_arm64.unsupported_windows_arm64_tool("gatk") is not None


def _write_pe(path: Path, machine: int) -> None:
    header = bytearray(64)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    path.write_bytes(header + b"PE\0\0" + struct.pack("<H", machine) + bytes(18))


@pytest.mark.parametrize("machine", [0x8664, 0x014C, 0xA641, 0xA64E])
def test_native_audit_rejects_emulated_and_hybrid_pe(
    machine: int, tmp_path: Path
) -> None:
    binary = tmp_path / "tool.dll"
    _write_pe(binary, machine)
    with pytest.raises(ValueError, match="native ARM64 requires"):
        windows_arm64.assert_arm64_pe(binary)


def test_native_audit_accepts_only_arm64_and_rejects_truncation(tmp_path: Path) -> None:
    binary = tmp_path / "tool.exe"
    _write_pe(binary, 0xAA64)
    windows_arm64.assert_arm64_pe(binary)
    binary.write_bytes(binary.read_bytes()[:-1])
    with pytest.raises(ValueError, match="missing PE/COFF"):
        windows_arm64.assert_arm64_pe(binary)


@pytest.mark.parametrize("content", [b"", b"not a PE", b"MZ" + bytes(62)])
def test_native_audit_rejects_invalid_headers(content: bytes, tmp_path: Path) -> None:
    binary = tmp_path / "tool.exe"
    binary.write_bytes(content)
    with pytest.raises(ValueError):
        windows_arm64.assert_arm64_pe(binary)


def test_arm64_runtime_rejects_configured_x64_binary_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    binary = tmp_path / "bwa.exe"
    _write_pe(binary, 0x8664)
    with pytest.raises(WGSExtractError, match="native ARM64 requires"):
        utils._normalize_subprocess_cmd([binary, "mem"])
    _write_pe(binary, 0xAA64)
    assert utils._normalize_subprocess_cmd([binary, "mem"]) == [str(binary), "mem"]


@pytest.mark.parametrize("wrapper", ["wsl.exe", "bash.exe", "docker.exe"])
def test_arm64_runtime_rejects_non_native_tool_substitutes(
    wrapper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    with pytest.raises(ValueError, match="tool substitutes"):
        windows_arm64.assert_native_windows_command([wrapper, "samtools"])


def test_arm64_version_probe_does_not_launch_x64_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    binary = tmp_path / "bwa.exe"
    _write_pe(binary, 0x8664)
    monkeypatch.setattr(dependency_checks, "get_tool_path", lambda _: str(binary))
    launch = Mock(side_effect=AssertionError("must not launch x64 executable"))
    monkeypatch.setattr(subprocess, "run", launch)
    assert "native ARM64 requires" in str(dependency_checks.get_tool_version("bwa"))
    launch.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows installer")
def test_native_arm64_installer_refuses_before_bootstrap() -> None:
    if windows_arm64.windows_native_machine() != 0xAA64:
        pytest.skip("requires Windows ARM64")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(root / "install_windows.bat"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "win-arm64" in result.stdout
    assert "Ensuring Pixi" not in result.stdout
