"""Exercise native launcher extraction without building the scientific image."""

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
