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
@pytest.mark.parametrize(
    "parent_path", [True, False], ids=["with-path", "without-path"]
)
def test_linux_arm_reaches_normal_dependency_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    architecture: str,
    parent_path: bool,
) -> None:
    if not parent_path:
        monkeypatch.delenv("PATH", raising=False)
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
        "PATH": os.pathsep.join(
            path for path in (str(commands), os.environ.get("PATH", os.defpath)) if path
        ),
        "WGSEXTRACT_INSTALL_DIR": str(installation),
        "WGSEXTRACT_ARCHIVE_URL": "https://example.invalid/source.tar.gz",
        "PIXI": str(tmp_path / "missing-pixi"),
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
    assert "PIXI is set but is not executable" in result.stderr
    assert "Native Linux ARM64" not in result.stderr
    assert not marker.exists()
    assert not installation.exists()


@pytest.mark.parametrize("architecture", ["aarch64", "arm64"])
def test_linux_arm_pixi_asset_is_native(tmp_path: Path, architecture: str) -> None:
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text(
        f'#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo {architecture};; esac\n'
    )
    uname.chmod(0o755)
    source = (ROOT / "install.sh").read_text()
    function = source.split("pixi_asset_name() {", 1)[1].split(
        "\nmake_staging_directory()", 1
    )[0]
    result = subprocess.run(
        [str(SH), "-c", "pixi_asset_name() {" + function + "\npixi_asset_name"],
        env={"PATH": str(commands)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "pixi-aarch64-unknown-linux-musl.tar.gz"
