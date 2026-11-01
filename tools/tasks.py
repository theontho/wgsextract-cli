#!/usr/bin/env python3
"""Cross-platform developer tasks for GUI for CLI.

Usage: tools/tasks.py <action> [target] [--dry-run]

The Makefile (macOS/Linux) and make.ps1 (Windows) both delegate here, so every
host runs the same commands.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = "app"
IS_WINDOWS = sys.platform.startswith("win")
PYTHON = sys.executable
PYTHON_TEST_DIRS = ("tools/tests", "tools/localization/tests", "tools/packaging/tests")
# Drives the WGSExtract bundle's POSIX wrapper scripts.
POSIX_TEST_DIRS = ("tests/wgsextract",)

Command = list[str]


def bundle_root() -> str:
    return str(Path(os.environ.get("BUNDLE") or "examples/WGSExtract").resolve())


def web_port() -> str:
    return os.environ.get("PORT") or "8787"


def npm(*args: str) -> Command:
    return ["npm", "--prefix", APP_DIR, *args]


def python(*args: str) -> Command:
    return [PYTHON, *args]


def setup(_: str) -> list[Command]:
    return (
        [powershell("scripts/dev_setup.ps1")]
        if IS_WINDOWS
        else [["sh", "scripts/dev_setup.sh"]]
    )


def precheck(_: str) -> list[Command]:
    return [python("tools/precheck.py")]


def bundle_paths() -> list[str]:
    bundles = sorted(
        path
        for path in (REPO_ROOT / "examples").iterdir()
        if (path / "manifest.json").is_file()
    )
    bundles += sorted(
        path.parent
        for path in (REPO_ROOT / "tests" / "conformance").glob("*/manifest.json")
    )
    return [path.relative_to(REPO_ROOT).as_posix() for path in bundles]


def lint(_: str) -> list[Command]:
    return [
        npm("run", "check"),
        [
            "node",
            f"{APP_DIR}/dist/web/src/server/bundle-validate-cli.js",
            "--quiet",
            *bundle_paths(),
        ],
        python("tools/localization/lint_locales.py", "--strict"),
        python("-m", "compileall", "-q", "tools", "scripts"),
        [
            "npx",
            "--prefix",
            APP_DIR,
            "jscpd",
            "--config",
            ".jscpd.json",
            "--reporters",
            "console",
            ".",
        ],
    ]


def test(_: str) -> list[Command]:
    test_dirs = (
        PYTHON_TEST_DIRS if IS_WINDOWS else (*PYTHON_TEST_DIRS, *POSIX_TEST_DIRS)
    )
    return [
        npm("test"),
        *(python("-m", "unittest", "discover", "-s", path) for path in test_dirs),
    ]


def build(target: str) -> list[Command]:
    return {
        "web": [npm("run", "build")],
        "tauri": [npm("run", "tauri:build")],
    }[target]


def run(target: str) -> list[Command]:
    return {
        "web": [
            npm("run", "start", "--", "--bundle", bundle_root(), "--port", web_port())
        ],
        "dev": [
            npm("run", "dev", "--", "--bundle", bundle_root(), "--port", web_port())
        ],
        "tauri": [npm("run", "tauri:dev")],
    }[target]


def package(target: str) -> list[Command]:
    if target == "tauri" and IS_WINDOWS:
        return [powershell("tools/packaging/windows/package_tauri.ps1")]
    if IS_WINDOWS:
        raise SystemExit(f"package {target} runs on macOS or Linux hosts")
    return [python("tools/packaging/posix/package_release.py", target)]


def powershell(script: str) -> Command:
    executable = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell"
    return [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]


def clean(_: str) -> list[Command]:
    paths = [
        f"{APP_DIR}/dist",
        f"{APP_DIR}/.cache",
        f"{APP_DIR}/web/packagers/tauri/target",
        "out",
    ]
    return [python("-c", CLEAN_SCRIPT, *paths)]


CLEAN_SCRIPT = (
    "import shutil, sys\n"
    "for path in sys.argv[1:]:\n"
    "    shutil.rmtree(path, ignore_errors=True)\n"
    "    print(f'removed {path}')\n"
)


def ci(_: str) -> list[Command]:
    return [*lint(""), *test("")]


# action -> (handler, default target, allowed targets)
ACTIONS: dict[str, tuple[Callable[[str], list[Command]], str, tuple[str, ...]]] = {
    "setup": (setup, "", ()),
    "precheck": (precheck, "", ()),
    "lint": (lint, "", ()),
    "test": (test, "", ()),
    "ci": (ci, "", ()),
    "build": (build, "web", ("web", "tauri")),
    "run": (run, "web", ("web", "dev", "tauri")),
    "package": (package, "tauri", ("tauri", "tauri-windows-cross")),
    "clean": (clean, "", ()),
}


def resolve(action: str, target: str | None) -> list[Command]:
    if action not in ACTIONS:
        raise SystemExit(
            f"unknown action: {action}\nKnown actions: {', '.join(ACTIONS)}"
        )
    handler, default_target, targets = ACTIONS[action]
    chosen = target or default_target
    if targets and chosen not in targets:
        raise SystemExit(
            f"unknown {action} target: {chosen}\nKnown targets: {', '.join(targets)}"
        )
    if not targets and target:
        raise SystemExit(f"{action} does not take a target")
    return handler(chosen)


def execute(command: Command) -> int:
    executable = shutil.which(command[0]) or command[0]
    return subprocess.run(
        [executable, *command[1:]], cwd=REPO_ROOT, check=False
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=list(ACTIONS))
    parser.add_argument("target", nargs="?")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without running them."
    )
    args = parser.parse_args(argv)

    for command in resolve(args.action, args.target):
        print(f"+ {shlex.join(command)}", flush=True)
        if args.dry_run:
            continue
        status = execute(command)
        if status != 0:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
