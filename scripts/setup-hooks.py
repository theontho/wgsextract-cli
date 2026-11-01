#!/usr/bin/env python3
"""Install pre-commit and pre-push git hooks for this repo."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(command: str, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [command, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


HOOKS: list[tuple[str, str]] = [
    (
        "pre-commit",
        """#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
run_python() {
  if [ -n "${PYTHON:-}" ]; then
    $PYTHON "$@"
  elif command -v uv >/dev/null 2>&1; then
    uv run python "$@"
  else
    python "$@"
  fi
}
run_python scripts/verify-dev.py
run_python tools/tasks.py lint
""",
    ),
    (
        "pre-push",
        """#!/bin/sh
set -eu
cd "$(git rev-parse --show-toplevel)"
# Tests initialize their own repositories, not the repository being pushed.
unset $(git rev-parse --local-env-vars)
run_python() {
  if [ -n "${PYTHON:-}" ]; then
    $PYTHON "$@"
  elif command -v uv >/dev/null 2>&1; then
    uv run python "$@"
  else
    python "$@"
  fi
}
run_python scripts/verify-dev.py
run_python tools/tasks.py test
""",
    ),
]


def hooks_dir(root: Path) -> Path:
    status, output = run("git", ["rev-parse", "--git-path", "hooks"])
    if status != 0:
        return root / ".git" / "hooks"
    path = Path(output.strip())
    return path if path.is_absolute() else root / path


def check_hooks(root: Path) -> bool:
    git_hooks_dir = hooks_dir(root)
    missing_or_stale: list[str] = []
    for name, body in HOOKS:
        path = git_hooks_dir / name
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            missing_or_stale.append(name)
            continue
        if current != body or not os.access(path, os.X_OK):
            missing_or_stale.append(name)

    if missing_or_stale:
        print("Git hooks missing or stale: " + ", ".join(missing_or_stale))
        print("Run 'uv run python scripts/setup-hooks.py' to install them.")
        return False

    print("Git hooks are installed.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Verify hooks are installed and current."
    )
    args = parser.parse_args()

    status, output = run("git", ["rev-parse", "--show-toplevel"])
    if status != 0:
        if args.check:
            print("Repository hooks check failed: not inside a Git repository.")
            return 1
        print("Skipping hook install: not inside a Git repository.")
        return 0

    root = Path(output.strip())
    if args.check:
        return 0 if check_hooks(root) else 1

    git_hooks_dir = hooks_dir(root)
    git_hooks_dir.mkdir(parents=True, exist_ok=True)

    for name, body in HOOKS:
        path = git_hooks_dir / name
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)

    print("Installed pre-commit and pre-push hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
