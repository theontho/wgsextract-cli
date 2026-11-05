"""scripts/test_smells.py flags change detectors only in newly added test lines."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_smells.py"

OLD = """from unittest.mock import patch

from wgsextract_cli.core import utils


def test_old(monkeypatch):
    monkeypatch.setattr(utils, "_old_private", None)
    monkeypatch.setattr(
        utils,
        "public_helper",
        None,
    )
"""

NEW = """
def test_new(monkeypatch, run, cmd):
    monkeypatch.setattr(utils, "_new_private", None)
    monkeypatch.setattr(utils, "public_helper", None)
    patch("wgsextract_cli.core.utils._dotted")
    assert cmd == ["samtools", "sort", "-o", "x.bam"]
    run.assert_called_once_with(["tabix", "-l", "x.vcf.gz"], check=True)
    assert cmd[:3] == ["samtools", "sort", "-m"]  # contract: -m is per-thread RAM
    assert cmd == ["samtools", "index", "x.bam"]
    # contract: -m sets the documented per-thread memory
    assert cmd[:3] == ["samtools", "sort", "-m"]
    assert subprocess.run(["samtools", "view", "x.bam"]).returncode == 0
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _smells(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, SCRIPT, "--base", "HEAD", "--root", root],
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(OLD)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "diff.mnemonicPrefix", "true")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        *("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"),
    )
    return tests


def test_reports_only_added_private_patches_and_argv_asserts(tmp_path: Path) -> None:
    tests = _repo(tmp_path)
    edited = OLD.replace('"public_helper"', '"_renamed_private"') + NEW
    (tests / "test_x.py").write_text(edited)

    result = _smells(tmp_path)

    assert result.returncode == 0
    findings = [line for line in result.stdout.splitlines() if ": TS" in line]
    assert findings == [
        "tests/test_x.py:8: TS001 patches private internal utils._renamed_private",
        "tests/test_x.py:15: TS001 patches private internal utils._new_private",
        "tests/test_x.py:17: TS001 patches private internal "
        "wgsextract_cli.core.utils._dotted",
        "tests/test_x.py:18: TS002 asserts a whole tool argv",
        "tests/test_x.py:19: TS002 asserts a whole tool argv",
        "tests/test_x.py:21: TS002 asserts a whole tool argv",
    ]


def test_new_untracked_files_are_checked_and_broken_ones_skipped(
    tmp_path: Path,
) -> None:
    tests = _repo(tmp_path)
    (tests / "test_new.py").write_text(OLD)
    (tests / "test_broken.py").write_text("def test_(:\n")

    result = _smells(tmp_path)

    assert result.returncode == 0
    assert "tests/test_new.py:7: TS001" in result.stdout
    assert "tests/test_x.py" not in result.stdout
    assert "skipped tests/test_broken.py" in result.stdout
