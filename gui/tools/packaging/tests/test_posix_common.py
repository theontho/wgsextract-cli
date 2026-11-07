from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.packaging.posix import common


class TestPosixCommon(unittest.TestCase):
    def test_copy_path_can_copy_ignored_build_artifact_directories(self) -> None:
        original_repo_root = common.REPO_ROOT
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name) / "repo"
            app_dir = repo_root / "build" / "WGSExtract.app"
            binary = app_dir / "Contents" / "MacOS" / "WGSExtract"
            dest_dir = repo_root / "out" / "WGSExtract.app"
            binary.parent.mkdir(parents=True)
            binary.write_text("binary\n", encoding="utf-8")
            (repo_root / ".gitignore").write_text("build/\n", encoding="utf-8")

            common.REPO_ROOT = repo_root
            try:
                common.copy_path(app_dir, dest_dir, git_filtered=False)
            finally:
                common.REPO_ROOT = original_repo_root

            copied_binary = dest_dir / "Contents" / "MacOS" / "WGSExtract"
            self.assertEqual(copied_binary.read_text(encoding="utf-8"), "binary\n")


if __name__ == "__main__":
    unittest.main()
