import os
import shutil
import subprocess
import sys
import tarfile
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


@pytest.mark.parametrize("architecture", ["aarch64", "arm64"])
@pytest.mark.parametrize(
    ("declaration", "supported", "existing_app"),
    [
        ('platforms = ["linux-64"]', False, False),
        ('platforms = ["linux-64"]', False, True),
        ('platforms = ["linux-aarch64-other"]', False, True),
        ('platforms = ["linux-64"] # "linux-aarch64"', False, True),
        (
            'platforms = ["linux-64"]\n[feature.optional]\n'
            'platforms = ["linux-aarch64"]',
            False,
            True,
        ),
        ('platforms = ["linux-64", "linux-aarch64", "win-64"]', True, False),
        ('platforms = ["linux-64", "linux-aarch64", "win-64"]', True, True),
        ("platforms = ['linux-aarch64', 'linux-64']", True, True),
        ('platforms = [\n "linux-aarch64",\n]', True, False),
        ('platforms = ["linux-64",\n "linux-aarch64"]', True, True),
        (
            "platforms = [ # a comment with ]\n"
            ' "linux-64", # another ]\n "linux-aarch64",\n]',
            True,
            False,
        ),
        ('platforms = [\n "linux-64",\n]', False, False),
        (
            'platforms = [\n "linux-aarch64-other",\n'
            ' # "linux-aarch64"\n]\n[feature.optional]\n'
            'platforms = ["linux-aarch64"]',
            False,
            True,
        ),
        ('[feature.optional]\nplatforms = ["linux-aarch64"]', False, False),
    ],
)
def test_linux_arm_validates_downloaded_ref_before_pixi_or_app_changes(
    tmp_path: Path,
    architecture: str,
    declaration: str,
    supported: bool,
    existing_app: bool,
) -> None:
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text(
        f'#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo {architecture};; esac\n'
    )
    uname.chmod(0o755)
    network_marker = tmp_path / "network-invoked"
    curl = commands / "curl"
    curl.write_text(f"#!/bin/sh\nprintf invoked > '{network_marker}'\nexit 99\n")
    curl.chmod(0o755)
    pixi_marker = tmp_path / "pixi-invoked"
    if supported:
        pixi = commands / "pixi"
        pixi.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> \'{pixi_marker}\'\n')
        pixi.chmod(0o755)

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "pixi.toml").write_text("[workspace]\n" + declaration + "\n")
    shutil.copy(ROOT / "uninstall.sh", payload / "uninstall.sh")
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="wgsextract-cli")

    installation = tmp_path / "installation"
    app = installation / "app"
    launcher = installation / "wgsextract"
    if existing_app:
        app.mkdir(parents=True)
        (app / "existing-app").write_text("preserve until payload is validated")
        launcher.write_text("existing launcher")
    before = {
        path.relative_to(installation): path.read_bytes() if path.is_file() else None
        for path in installation.rglob("*")
    }
    home = tmp_path / "home"
    home.mkdir()
    ref = "main" if supported else "v0.6.5"
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith("WGSEXTRACT_")
        },
        "HOME": str(home),
        "PATH": os.pathsep.join((str(commands), os.defpath)),
        "WGSEXTRACT_INSTALL_DIR": str(installation),
        "WGSEXTRACT_ARCHIVE_URL": str(archive),
        "WGSEXTRACT_REF": ref,
        "PIXI": "",
    }
    result = subprocess.run(
        [str(SH), str(ROOT / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert not network_marker.exists()
    assert not list(installation.glob(".source-download*"))
    if supported:
        assert result.returncode == 0, result.stderr
        assert "install --locked" in pixi_marker.read_text()
        assert (app / "pixi.toml").read_text() == "[workspace]\n" + declaration + "\n"
        assert launcher.read_text() != "existing launcher"
        assert (installation / "uninstall.sh").exists()
        assert (installation / "install-manifest.json").exists()
    else:
        assert result.returncode == 1
        assert ref in result.stderr
        assert "No dependencies were installed" in result.stderr
        assert "WGSEXTRACT_REF=main" in result.stderr
        assert not pixi_marker.exists()
        assert not (home / ".pixi").exists()
        if not existing_app:
            assert not installation.exists()
        assert {
            path.relative_to(installation): path.read_bytes()
            if path.is_file()
            else None
            for path in installation.rglob("*")
        } == before
