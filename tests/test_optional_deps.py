"""Missing tools: what verification and `deps check` tell the user, and how
resolved tool commands are split into arguments."""

import logging
import os
import shlex
import subprocess
import sys
from argparse import Namespace
from types import SimpleNamespace

import pytest

from tests.fake_tools import python_tool
from wgsextract_cli.commands import _deps_status
from wgsextract_cli.core import dependency_checks, windows_arm64
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.dependency_checks import (
    check_all_dependencies,
    get_tool_version,
    verify_dependencies,
)
from wgsextract_cli.core.tool_commands import _split_windows_argv, _tool_command_parts
from wgsextract_cli.core.utils import MissingDependencyError

# Probing another environment sends pixi a multi-line `python -c` argument,
# which a batch-file pixi stand-in can't pass on.
posix_pixi = pytest.mark.skipif(
    sys.platform == "win32", reason="a batch file can't take a multi-line argument"
)


@pytest.fixture(autouse=True)
def _x64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows ARM64 hosts accept only qualified native tools, not batch files.
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0x8664)


@pytest.fixture
def host(fake_pixi, monkeypatch):
    """The host's programs folder, alone on PATH, with the CLI in the default
    environment, which has none of them; its lockfile has minimap2."""
    prefix = fake_pixi.env("default", locked=("minimap2",))
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    return fake_pixi.host_bin


def test_a_missing_optional_tool_names_the_command_that_installs_it(host, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(MissingDependencyError) as error:
        verify_dependencies(["minimap2"])

    message = str(error.value)
    assert message.startswith("Missing required optional tool(s): minimap2. Run: ")
    assert "install --locked" in message and message.endswith("-e default")
    assert "package manager" in caplog.text


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_missing_core_tool_stops_the_command(host, monkeypatch, platform):
    """Windows runs its tools from Pixi like everywhere else: there is no
    runtime to fall back to."""
    monkeypatch.setattr(dependency_checks, "sys", SimpleNamespace(platform=platform))

    with pytest.raises(MissingDependencyError) as error:
        verify_dependencies(["samtools", "minimap2"])

    assert str(error.value) == "Missing required core tool(s): samtools, minimap2"


def test_python_runtime_is_attributed_to_pixi_inside_a_pixi_env(tmp_path, monkeypatch):
    prefix = tmp_path / "envs" / "default"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    for name in [n for n in os.environ if n.startswith("PIXI_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("CONDA_PREFIX", str(prefix))
    monkeypatch.setenv("PIXI_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "executable", str(python))

    results = check_all_dependencies(mandatory=[], optional=[])

    assert results["mandatory"][0]["name"] == "Python Runtime"
    assert results["mandatory"][0]["runtime"] == "pixi"


@posix_pixi
def test_the_install_command_deps_check_offers_makes_the_tool_available(
    host, fake_pixi, capsys
):
    prefix = fake_pixi.env("vep", locked=("ensembl-vep",), installed=False)
    python_tool(prefix / "bin", "vep", "")

    with pytest.raises(MissingDependencyError) as error:
        _deps_status.run(Namespace(tool="vep", debug=False))
    message = str(error.value)
    assert message.startswith("Tool not found: vep. Run: ")
    elsewhere = host.parent / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(shlex.split(message.split("Run: ", 1)[1]), cwd=elsewhere, check=True)
    clear_tool_caches()  # as a new CLI process would start

    _deps_status.run(Namespace(tool="vep", debug=False))
    assert capsys.readouterr().out.strip()


@posix_pixi
def test_no_install_command_for_a_tool_this_platforms_lockfile_lacks(host, fake_pixi):
    """pbmm2's environment exists here, but not pbmm2 (PacBio ships it for
    Linux only)."""
    fake_pixi.env("pacbio", locked=("sniffles",), installed=False)

    with pytest.raises(MissingDependencyError) as error:
        _deps_status.run(Namespace(tool="pbmm2", debug=False))

    assert str(error.value) == "Tool not found: pbmm2"


def test_a_tool_in_a_folder_with_spaces_runs(host, monkeypatch):
    folder = host.parent / "Program Files" / "tools"
    python_tool(folder, "samtools", 'print("samtools 1.21")')
    monkeypatch.setenv("PATH", str(folder))
    assert get_tool_version("samtools") == "samtools 1.21"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-line parsing")
def test_windows_pixi_command_parts_preserve_backslashes():
    command = (
        r"C:\Users\test\.pixi\bin\pixi.EXE run -e default perl "
        r"C:\repo\.pixi\envs\default\opt\fastqc-0.12.1\fastqc"
    )

    assert _tool_command_parts(command) == [
        r"C:\Users\test\.pixi\bin\pixi.EXE",
        "run",
        "-e",
        "default",
        "perl",
        r"C:\repo\.pixi\envs\default\opt\fastqc-0.12.1\fastqc",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [r"C:\Users\lab user\.pixi\bin\pixi.exe", "run", "-c", "print('a b')"],
        [r"C:\Users\bob\pixi.exe", r"C:\a\b", 'q"uote', "trail\\", ""],
    ],
)
def test_windows_argv_split_inverts_list2cmdline(argv):
    assert _split_windows_argv(subprocess.list2cmdline(argv)) == argv
