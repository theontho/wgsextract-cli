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


if __name__ == "__main__":
    unittest.main()
