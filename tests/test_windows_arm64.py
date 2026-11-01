import argparse
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.core import dependency_checks, pixi_tools, utils, windows_arm64
from wgsextract_cli.core.utils import WGSExtractError


@pytest.mark.skipif(
    windows_arm64.windows_native_machine() != 0xAA64,
    reason="requires a native Windows ARM64 process",
)
def test_native_process_audit_drains_both_full_output_pipes(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts"
    script /= "qualify_windows_arm64_process.ps1"
    shutil.copyfile(sys.executable, tmp_path / "native-python.exe")
    report = tmp_path / "audit.json"
    code = (
        "import sys; sys.stdin.read(); "
        "sys.stdout.write('x'*65536); sys.stderr.write('y'*65536)"
    )

    def quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    command = (
        f"& {quote(script)} -Executable {quote(sys.executable)} "
        f"-Arguments @('-u', '-c', {quote(code)}) "
        f"-Prefix {quote(tmp_path)} -Report {quote(report)}"
    )
    subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert Path(str(report) + ".stdout.log").read_bytes() == b"x" * 65536
    assert Path(str(report) + ".stderr.log").read_bytes() == b"y" * 65536


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


@pytest.mark.parametrize("category", ["mandatory", "optional"])
def test_arm64_dependency_report_explains_unsupported_tools_without_install_hint(
    category: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    monkeypatch.setattr(dependency_checks, "get_tool_path", lambda tool: None)
    install = Mock(side_effect=AssertionError("must not suggest an x64 environment"))
    monkeypatch.setattr(dependency_checks, "pixi_install_command", install)
    arguments: dict[str, list[str]] = {"mandatory": [], "optional": []}
    arguments[category] = ["gatk", "run_deepvariant"]
    results = dependency_checks.check_all_dependencies(**arguments)
    reports = [row for row in results[category] if row["name"] != "Python Runtime"]
    assert len(reports) == 2
    assert all("native Windows ARM64" in row["version"] for row in reports)
    assert all(row["path"] is None for row in reports)
    install.assert_not_called()


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
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
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


def test_simulated_windows_does_not_infer_os_architecture_from_an_arm_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(windows_arm64.ctypes, "WinDLL", None, raising=False)
    host = Mock()
    host.name = "posix"
    monkeypatch.setattr(windows_arm64, "os", host)
    assert windows_arm64.windows_native_machine() is None
    host.name = "nt"
    with pytest.raises(OSError, match="architecture query is unavailable"):
        windows_arm64.windows_native_machine()


def _write_pe(path: Path, machine: int) -> None:
    header = bytearray(64)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    path.write_bytes(header + b"PE\0\0" + struct.pack("<H", machine) + bytes(18))


@pytest.fixture
def _native_arm64_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    python = tmp_path / "native-python.exe"
    _write_pe(python, 0xAA64)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(python))


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _native_arm64_python: None
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
    wrapper: str, monkeypatch: pytest.MonkeyPatch, _native_arm64_python: None
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    with pytest.raises(ValueError, match="tool substitutes"):
        windows_arm64.assert_native_windows_command([wrapper, "samtools"])


def test_arm64_version_probe_does_not_launch_x64_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _native_arm64_python: None
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    binary = tmp_path / "bwa.exe"
    _write_pe(binary, 0x8664)
    monkeypatch.setattr(dependency_checks, "get_tool_path", lambda _: str(binary))
    launch = Mock(side_effect=AssertionError("must not launch x64 executable"))
    monkeypatch.setattr(subprocess, "run", launch)
    assert "native ARM64 requires" in str(dependency_checks.get_tool_version("bwa"))
    launch.assert_not_called()


@pytest.mark.parametrize(
    "arguments", [["--format", "fasta"], ["--format=fasta"], ["--write-fasta-msa"]]
)
def test_arm64_haplogrep_rejects_foreign_jbwa_before_java(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _native_arm64_python: None,
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    java = tmp_path / "java.exe"
    _write_pe(java, 0xAA64)
    command = [str(java), "-jar", "haplogrep.jar", "classify"]
    with pytest.raises(WGSExtractError, match="JBWA JNI kernels are not qualified"):
        utils._normalize_subprocess_cmd([*command, *arguments])
    allowed = [*command, "--format", "vcf"]
    assert utils._normalize_subprocess_cmd(allowed) == allowed
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0x8664)
    assert utils._normalize_subprocess_cmd([*command, *arguments]) == [
        *command,
        *arguments,
    ]


def test_arm64_features_reject_prism_python_before_input_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    python = tmp_path / "python.exe"
    _write_pe(python, 0x8664)
    monkeypatch.setattr(sys, "executable", str(python))
    with pytest.raises(WGSExtractError, match="Prism-emulated Python"):
        verify_platform_support(argparse.Namespace(command="info"))


def test_arm64_discovery_and_wrappers_do_not_start_foreign_environments(
    monkeypatch: pytest.MonkeyPatch, _native_arm64_python: None
) -> None:
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0xAA64)
    launch = Mock(side_effect=AssertionError("must not probe foreign Python"))
    monkeypatch.setattr(subprocess, "run", launch)
    assert pixi_tools._probe_environment_with_pixi("pixi", "bio-tools", ["bwa"]) is None
    with pytest.raises(ValueError, match="cross-environment Pixi wrappers"):
        windows_arm64.assert_native_windows_command(
            ["pixi", "run", "-e", "bio-tools", "bwa"]
        )
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
