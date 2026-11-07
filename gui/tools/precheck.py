#!/usr/bin/env python3
"""Check repository development environment readiness."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

CHECK_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CheckResult:
    label: str
    passed: bool
    detail: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress passing check output."
    )
    parser.add_argument("--repo-root", default=".", help="Repository root to check.")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    print_line("Running precheck...", quiet=args.quiet)
    checks = [
        check_command("Node.js", ["node", "--version"], cwd=repo_root),
        check_command("npm", ["npm", "--version"], cwd=repo_root),
        check_npm_dependencies(repo_root),
        check_command(
            "Rust Cargo (Tauri builds)", ["cargo", "--version"], cwd=repo_root
        ),
        check_config_directory(),
    ]
    for check in checks:
        prefix = "OK" if check.passed else "FAIL"
        detail = f" - {check.detail}" if check.detail else ""
        print_line(f"{prefix} {check.label}{detail}", quiet=args.quiet and check.passed)
    if all(check.passed for check in checks):
        print_line("Precheck passed.", quiet=args.quiet)
        return 0
    return 1


def check_command(label: str, command: list[str], *, cwd: Path) -> CheckResult:
    executable = shutil.which(command[0])
    if executable is None:
        return CheckResult(label, False, f"{command[0]} was not found on PATH")
    try:
        result = subprocess.run(
            [executable, *command[1:]],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(label, False, f"timed out after {CHECK_TIMEOUT_SECONDS}s")
    except OSError as error:
        return CheckResult(label, False, str(error))
    return CheckResult(label, result.returncode == 0, first_line(result.stdout))


def check_npm_dependencies(repo_root: Path) -> CheckResult:
    modules = repo_root / "app" / "node_modules"
    if (modules / "typescript").is_dir():
        return CheckResult("npm dependencies", True, str(modules))
    return CheckResult(
        "npm dependencies",
        False,
        "run 'make setup' (or 'powershell -ExecutionPolicy Bypass -File .\\make.ps1 setup' on Windows)",
    )


def check_config_directory() -> CheckResult:
    config_dir = app_support_dir()
    if config_dir.exists():
        probe = config_dir / f".write-test-{uuid.uuid4()}"
        try:
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
            return CheckResult("Config directory", True, str(config_dir))
        except OSError as error:
            probe.unlink(missing_ok=True)
            return CheckResult("Config directory", False, str(error))
    parent = nearest_existing_parent(config_dir)
    writable = os.access(parent, os.W_OK)
    return CheckResult("Config directory parent", writable, str(parent))


def nearest_existing_parent(path: Path) -> Path:
    candidate = path.parent
    while not candidate.exists():
        next_candidate = candidate.parent
        if next_candidate == candidate:
            return candidate
        candidate = next_candidate
    return candidate


def app_support_dir() -> Path:
    override = os.environ.get("GUI_FOR_CLI_APP_SUPPORT_NAME")
    name = safe_path_component(override) if override else "gui-for-cli"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        return (
            Path(local_app_data)
            if local_app_data
            else Path.home() / "AppData" / "Local"
        ) / name
    data_home = os.environ.get("XDG_DATA_HOME")
    return (
        Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    ) / name


def safe_path_component(value: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in "-_." else "-"
        for character in value
    )
    trimmed = sanitized.strip(".-")
    return trimmed or "bundle"


def first_line(value: str) -> str:
    return value.splitlines()[0] if value.splitlines() else ""


def print_line(value: str, *, quiet: bool) -> None:
    if not quiet:
        print(value)


if __name__ == "__main__":
    raise SystemExit(main())
