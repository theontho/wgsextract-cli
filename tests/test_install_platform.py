import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(
    SH is None or sys.platform == "win32",
    reason="POSIX installer platform guard",
)


@pytest.mark.parametrize("architecture", ["aarch64", "arm64"])
def test_linux_arm_fails_before_network_or_dependency_setup(tmp_path, architecture):
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text(
        f'#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo {architecture};; esac\n',
        encoding="utf-8",
        newline="\n",
    )
    uname.chmod(0o755)
    marker = tmp_path / "unexpected-command"
    for name in ("curl", "tar", "gzip", "pixi"):
        command = commands / name
        command.write_text(
            f"#!/bin/sh\nprintf invoked > '{marker}'\nexit 99\n",
            encoding="utf-8",
            newline="\n",
        )
        command.chmod(0o755)
    installation = tmp_path / "installation"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "WGSEXTRACT_INSTALL_DIR": str(installation),
        "PIXI_ARCH": "x86_64",
    }
    result = subprocess.run(
        [str(SH), str(ROOT / "install.sh")],
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Native Linux ARM64 (aarch64) is not supported yet" in result.stderr
    assert "No dependencies were installed" in result.stderr
    assert not marker.exists()
    assert not installation.exists()
