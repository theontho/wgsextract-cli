"""Exercise native launcher extraction without building the scientific image."""

import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PATCHER = (
    Path(__file__).resolve().parents[1]
    / "packaging"
    / "deepvariant-arm64"
    / "patch_launchers.py"
)


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("unzip") is None,
    reason="native Linux extractor requires POSIX unzip/symlinks",
)
@pytest.mark.parametrize("valid_archive", [True, False])
def test_native_extractor_handles_spaces_symlinks_and_errors(tmp_path, valid_archive):
    patch = runpy.run_path(str(PATCHER))["patch_release_script"]
    source = (
        "import os, sys\n"
        "zip_path, dest_dir = sys.argv[1:]\n"
        'os.system("unzip -qq " + zip_path + " -d " + dest_dir)\n'
    )
    launcher = tmp_path / "launcher.py"
    launcher.write_text(patch(source))
    archive = tmp_path / "model runfiles.zip"
    destination = tmp_path / "my results"
    if valid_archive:
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("payload", "model")
            link = zipfile.ZipInfo("native module")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(link, "payload")
    else:
        archive.write_text("invalid zip")
    result = subprocess.run(
        [sys.executable, str(launcher), str(archive), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if valid_archive:
        assert result.returncode == 0, result.stderr
        assert (destination / "native module").is_symlink()
        assert (destination / "native module").read_text() == "model"
    else:
        assert result.returncode != 0
        assert "CalledProcessError" in result.stderr


def test_native_extractor_rejects_changed_upstream_scaffold():
    patch = runpy.run_path(str(PATCHER))["patch_release_script"]
    with pytest.raises(ValueError, match="audit before building"):
        patch("unknown upstream extractor")


@pytest.mark.skipif(sys.platform == "win32", reason="native Linux build shell")
def test_native_build_uses_one_loaded_multistage_image(tmp_path, monkeypatch):
    commands = tmp_path / "docker-commands.jsonl"
    tools = tmp_path / "tools"
    tools.mkdir()
    stub = tools / "stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "tool, args = Path(sys.argv[0]).name, sys.argv[1:]\n"
        "if tool == 'uname':\n"
        "    print('Linux' if args == ['-s'] else 'aarch64')\n"
        "elif tool == 'git':\n"
        "    if args[0] == 'clone':\n"
        "        scripts = Path(args[-1]) / 'scripts/build'\n"
        "        scripts.mkdir(parents=True)\n"
        "        (scripts / 'build-prereq-arm64.sh').write_text('')\n"
        "        (scripts / 'build_release_binaries_arm64.sh').write_text("
        '\'os.system("unzip -qq " + zip_path + " -d " + dest_dir)\')\n'
        "    elif args == ['rev-parse', 'HEAD']:\n"
        "        print('ff2a2a949c8dd244908499ceb6a9fda381fcccf6')\n"
        "    elif args == ['rev-parse', 'FETCH_HEAD']:\n"
        "        print('d6b15e21323d9d15b347083f1d256ad62998c4cf')\n"
        "elif tool == 'docker':\n"
        f"    with open({str(commands)!r}, 'a') as log:\n"
        "        log.write(json.dumps(args) + '\\n')\n"
        "    if args[0] == 'info':\n"
        "        print('aarch64')\n"
        "    elif args[:2] == ['image', 'inspect']:\n"
        "        print('sha256:local-build' if '{{.Id}}' in args else 'linux/arm64')\n"
    )
    stub.chmod(0o755)
    for tool in ("git", "uname", "docker"):
        (tools / tool).symlink_to(stub)
    (tools / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    work = tmp_path / "build context"
    subprocess.run(
        ["bash", str(PATCHER.with_name("build.sh")), str(work)],
        check=True,
        capture_output=True,
        text=True,
    )
    builds = [
        args
        for line in commands.read_text().splitlines()
        if (args := json.loads(line))[0] == "build"
    ]
    (build,) = builds
    assert "--load" in build
    assert build[build.index("-t") + 1] == (
        "localhost/wgsextract-deepvariant:1.10.0-arm64"
    )
    dockerfile = Path(build[build.index("-f") + 1])
    assert [
        line for line in dockerfile.read_text().splitlines() if line.startswith("FROM ")
    ] == ["FROM arm64v8/ubuntu:24.04 AS builder", "FROM builder AS runtime"]
    assert (work / "image-id.txt").read_text().strip() == "sha256:local-build"
