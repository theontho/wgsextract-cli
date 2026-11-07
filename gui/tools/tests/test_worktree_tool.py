from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tools import worktree


class WorktreeToolTests(unittest.TestCase):
    def test_parse_worktrees_keeps_branch_and_paths_with_spaces(self) -> None:
        entries = worktree.parse_worktrees(
            "\n".join(
                (
                    "worktree /tmp/gui-for-cli",
                    "HEAD abc123",
                    "branch refs/heads/main",
                    "",
                    "worktree /tmp/gui for cli feature",
                    "HEAD def456",
                    "branch refs/heads/feature/worktree-setup",
                    "",
                )
            )
        )

        self.assertEqual(
            entries,
            [
                worktree.WorktreeEntry(Path("/tmp/gui-for-cli"), "main"),
                worktree.WorktreeEntry(Path("/tmp/gui for cli feature"), "feature/worktree-setup"),
            ],
        )

    def test_default_worktree_path_uses_sibling_root_and_safe_branch_name(self) -> None:
        root = Path("/Users/dev/src/gui-for-cli")

        self.assertEqual(
            worktree.default_worktree_path(root, "feature/worktree setup", ""),
            Path("/Users/dev/src/gui-for-cli-worktrees/feature-worktree-setup"),
        )

    def test_env_value_ignores_empty_make_exports(self) -> None:
        with mock.patch.dict(os.environ, {"WORKTREE_BASE": "", "BASE": ""}, clear=False):
            self.assertEqual(
                worktree.env_value("WORKTREE_BASE", "BASE", default="origin/main"),
                "origin/main",
            )

    def test_setup_command_uses_powershell_on_windows(self) -> None:
        path = Path("C:/src/gui-for-cli-worktrees/my-feature")
        powershell = "C:/Program Files/PowerShell/7/pwsh.exe"

        with (
            mock.patch.object(worktree.sys, "platform", "win32"),
            mock.patch.object(
                worktree.shutil,
                "which",
                side_effect=lambda name: powershell if name == "pwsh" else None,
            ),
        ):
            self.assertEqual(
                worktree.setup_command(path),
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(path / "make.ps1"),
                    "setup",
                ],
            )

    def test_setup_command_uses_make_outside_windows(self) -> None:
        with mock.patch.object(worktree.sys, "platform", "darwin"):
            self.assertEqual(worktree.setup_command(Path("/tmp/worktree")), ["make", "setup"])

    def test_run_developer_setup_runs_platform_setup_command(self) -> None:
        with (
            mock.patch.object(worktree, "setup_command", return_value=["powershell", "setup"]),
            mock.patch.object(worktree, "run_checked") as run_checked,
        ):
            worktree.run_developer_setup(Path("C:/src/worktree"))

        run_checked.assert_called_once_with(["powershell", "setup"], cwd=Path("C:/src/worktree"))


if __name__ == "__main__":
    unittest.main()
