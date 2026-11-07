"""Git hooks must not leak the outer repository into test repositories."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "setup_hooks", ROOT / "scripts/setup-hooks.py"
)
assert SPEC and SPEC.loader
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)


class PrePushTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == "nt" or not shutil.which("sh"), "POSIX hook environment"
    )
    def test_test_runner_can_initialize_a_different_repository(self) -> None:
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="hook test-", dir=ROOT / "tmp"
        ) as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
            probe = root / "probe"
            runner = root / "python-probe"
            runner.write_text(
                "#!/bin/sh\nset -eu\n"
                'if [ "$1" = "scripts/verify-dev.py" ]; then\n'
                '  git init "$PROBE" >/dev/null 2>&1\n'
                '  [ "$(git -C "$PROBE" rev-parse --show-toplevel)" = "$PROBE" ]\n'
                "fi\n",
                encoding="utf-8",
            )
            runner.chmod(0o755)
            hook = root / ".git/hooks/pre-push"
            hook.write_text(dict(hooks.HOOKS)["pre-push"], encoding="utf-8")
            result = subprocess.run(
                ["sh", str(hook)],
                cwd=root,
                env={
                    **os.environ,
                    "GIT_DIR": str(root / ".git"),
                    "PYTHON": runner.name,
                    "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PROBE": str(probe),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((probe / ".git").is_dir())


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"), "POSIX hook environment")
class PreCommitSecretScanTests(unittest.TestCase):
    def run_pre_commit(self, path: str) -> subprocess.CompletedProcess[str]:
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="hook test-", dir=ROOT / "tmp"
        ) as directory:
            root = Path(directory)
            subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
            # Split so this file doesn't trip secret scanners itself.
            token = "ghp_" + "1a2B3c4D5e6F7g8H9i0JkLmNoPqRsTuVwXyZ01"
            (root / "leak.py").write_text(f'token = "{token}"\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "leak.py"], check=True)
            runner = root / "python-probe"
            runner.write_text("#!/bin/sh\n", encoding="utf-8")
            runner.chmod(0o755)
            hook = root / ".git/hooks/pre-commit"
            hook.write_text(dict(hooks.HOOKS)["pre-commit"], encoding="utf-8")
            return subprocess.run(
                [shutil.which("sh") or "sh", str(hook)],
                cwd=root,
                env={
                    **os.environ,
                    "PYTHON": runner.name,
                    "PATH": f"{root}{os.pathsep}{path}",
                },
                capture_output=True,
                text=True,
                check=False,
            )

    @unittest.skipIf(not shutil.which("gitleaks"), "gitleaks is not installed")
    def test_staged_secret_blocks_commit(self) -> None:
        result = self.run_pre_commit(os.environ.get("PATH", ""))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("leaks found", result.stdout + result.stderr)

    def test_missing_gitleaks_warns_without_blocking(self) -> None:
        git = shutil.which("git")
        assert git
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as bin_dir:
            os.symlink(git, Path(bin_dir) / "git")
            result = self.run_pre_commit(bin_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gitleaks not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
