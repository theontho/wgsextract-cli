from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.packaging.git_filters import copy_git_filtered


@unittest.skipIf(shutil.which("git") is None, "git is required for git filter tests")
class TestGitFilters(unittest.TestCase):
    def test_copy_git_filtered_excludes_gitignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name) / "repo"
            src_dir = repo_root / "bundle"
            dest_dir = Path(tmp_dir_name) / "copied"

            (src_dir / "output").mkdir(parents=True)
            (src_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
            (src_dir / "notes.txt").write_text("include me\n", encoding="utf-8")
            (src_dir / "output/cache.bin").write_text("ignore me\n", encoding="utf-8")
            (repo_root / ".gitignore").write_text("bundle/output/\n", encoding="utf-8")

            self.run_git(repo_root, "init")
            self.run_git(repo_root, "add", ".gitignore", "bundle/manifest.json")

            success = copy_git_filtered(src_dir, dest_dir, repo_root)
            self.assertTrue(success)

            self.assertTrue((dest_dir / "manifest.json").exists())
            self.assertTrue((dest_dir / "notes.txt").exists())
            self.assertFalse((dest_dir / "output").exists())

    def test_copy_git_filtered_keeps_ignored_only_directory_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name) / "repo"
            src_dir = repo_root / "bundle"
            dest_dir = Path(tmp_dir_name) / "copied"

            (src_dir / "output").mkdir(parents=True)
            (src_dir / "output/cache.bin").write_text("ignore me\n", encoding="utf-8")
            (repo_root / ".gitignore").write_text("bundle/output/\n", encoding="utf-8")
            dest_dir.mkdir()
            (dest_dir / "stale.txt").write_text("remove me\n", encoding="utf-8")

            self.run_git(repo_root, "init")
            self.run_git(repo_root, "add", ".gitignore")

            success = copy_git_filtered(src_dir / "output", dest_dir, repo_root)
            self.assertTrue(success)
            self.assertEqual(list(dest_dir.iterdir()), [])

    def test_copy_git_filtered_does_not_copy_ignored_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name) / "repo"
            src_file = repo_root / "bundle/cache.bin"
            dest_file = Path(tmp_dir_name) / "cache.bin"

            src_file.parent.mkdir(parents=True)
            src_file.write_text("ignore me\n", encoding="utf-8")
            (repo_root / ".gitignore").write_text("bundle/cache.bin\n", encoding="utf-8")
            dest_file.write_text("stale\n", encoding="utf-8")

            self.run_git(repo_root, "init")
            self.run_git(repo_root, "add", ".gitignore")

            success = copy_git_filtered(src_file, dest_file, repo_root)
            self.assertTrue(success)
            self.assertFalse(dest_file.exists())

    def test_copy_git_filtered_refuses_destination_that_contains_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name) / "repo"
            src_dir = repo_root / "bundle"

            (src_dir / "manifest.json").parent.mkdir(parents=True)
            (src_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

            self.run_git(repo_root, "init")
            self.run_git(repo_root, "add", "bundle/manifest.json")

            self.assertFalse(copy_git_filtered(src_dir, repo_root, repo_root))
            self.assertTrue((src_dir / "manifest.json").exists())

    def test_copy_git_filtered_returns_false_for_external_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            root = Path(tmp_dir_name)
            repo_root = root / "repo"
            external = root / "external"
            repo_root.mkdir()
            external.mkdir()

            self.run_git(repo_root, "init")

            self.assertFalse(copy_git_filtered(external, root / "copied", repo_root))

    @staticmethod
    def run_git(repo_root: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
