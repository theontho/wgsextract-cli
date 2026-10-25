"""Finding a tool in a Pixi environment: in this process when the CLI already
runs in that environment (`pixi run` takes seconds on Windows), else inside it."""

import subprocess
import sys
from pathlib import Path

import pytest

from wgsextract_cli.core import pixi_tools


def _prefix(tmp_path: Path) -> Path:
    (tmp_path / "opt" / "fastqc-0.13.0").mkdir(parents=True)
    (tmp_path / "opt" / "fastqc-0.13.0" / "fastqc").write_text(
        "#!/usr/bin/env python3\n"
    )
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "haplogrep.jar").write_bytes(b"PK")
    (tmp_path / "bin" / "tool.sh").write_text("#!/bin/sh\n")
    return tmp_path


def test_probe_finds_scripts_jars_and_bin(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    prefix = str(_prefix(tmp_path))
    probe = pixi_tools._probe_environment
    fastqc = tmp_path / "opt" / "fastqc-0.13.0" / "fastqc"
    assert probe(prefix, ["fastqc"], windows=True) == f"script:{fastqc}"
    assert probe(prefix, ["haplogrep"], windows=False).startswith("haplogrep:")
    assert probe(prefix, ["tool.sh"], windows=False).endswith("tool.sh")
    assert probe(prefix, ["no-such-tool-anywhere"], windows=False) is None


def test_the_probe_runs_inside_another_environment(tmp_path, monkeypatch):
    # The source sent to the other environment's Python must run on its own.
    run = subprocess.run

    def fake_pixi(cmd, **kwargs):
        script, names = cmd[cmd.index("-c") + 1], cmd[cmd.index("-c") + 2 :]
        return run([sys.executable, "-c", script, *names], **kwargs)

    monkeypatch.setattr(pixi_tools.subprocess, "run", fake_pixi)
    found = pixi_tools._probe_environment_with_pixi("pixi", "default", ["python"])
    assert found is not None and found.stdout.strip() == "direct-name:python"
    missing = pixi_tools._probe_environment_with_pixi(
        "pixi", "default", ["no-such-tool-anywhere"]
    )
    assert missing is not None and missing.returncode == 1


@pytest.mark.parametrize("windows", [True, False])
def test_in_its_own_environment_the_cli_skips_pixi(tmp_path, monkeypatch, windows):
    prefix = _prefix(tmp_path)
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setattr(pixi_tools.sys, "prefix", str(prefix))
    monkeypatch.setattr(pixi_tools.runtime, "is_windows_host", lambda: windows)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "pixi")
    monkeypatch.setattr("shutil.which", lambda name: None)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("ran a subprocess")

    monkeypatch.setattr(pixi_tools.subprocess, "run", no_subprocess)
    command = pixi_tools._host_pixi_tool_command("fastqc")
    if windows:
        assert command is not None and " run " not in command
        assert command.startswith(sys.executable) and "fastqc-0.13.0" in command
    else:
        assert command is None  # Unix links the script into bin/; nothing here
