"""scripts/test_smells.py flags change detectors only in newly added test lines."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test_smells.py"

OLD = """from wgsextract_cli.core import utils


def test_old(monkeypatch):
    monkeypatch.setattr(utils, "_old_private", None)
"""

NEW = """
def test_new(monkeypatch):
    monkeypatch.setattr(utils, "_new_private", None)
    monkeypatch.setattr(utils, "public_helper", None)
    monkeypatch.setattr("wgsextract_cli.core.utils._dotted", None)
    assert cmd == ["samtools", "sort", "-o", "x.bam"]
    # contract: the -m flag sets the documented per-thread memory
    assert cmd[:3] == ["samtools", "sort", "-m"]
    assert ["a", "b"] == names
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_reports_new_private_patches_and_argv_asserts_without_failing(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "test_x.py"
    test_file.parent.mkdir()
    test_file.write_text(OLD)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        *("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"),
    )
    test_file.write_text(OLD + NEW)

    result = subprocess.run(
        [sys.executable, SCRIPT, "--base", "HEAD", "--root", tmp_path],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    findings = [line for line in result.stdout.splitlines() if ": TS" in line]
    assert findings == [
        "tests/test_x.py:8: TS001 patches private internal utils._new_private",
        "tests/test_x.py:10: TS001 patches private internal "
        "wgsextract_cli.core.utils._dotted",
        "tests/test_x.py:11: TS002 asserts a whole tool argv",
    ]
