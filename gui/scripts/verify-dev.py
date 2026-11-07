#!/usr/bin/env python3
"""Verify the local git identity matches the values stored in .dev_id."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


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


def git_config(key: str) -> Optional[str]:
    status, output = run("git", ["config", key])
    return output.strip() if status == 0 else None


def parse_dev_id(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in content.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    dev_id_path = ".dev_id"
    if not os.path.exists(dev_id_path):
        sys.stderr.write(
            "Error: .dev_id file not found. Run 'python3 scripts/dev-register.py'.\n"
        )
        return 1

    expected = parse_dev_id(dev_id_path)
    current_name = git_config("user.name")
    current_email = git_config("user.email")
    errors: list[str] = []

    if current_name != expected.get("name"):
        errors.append(
            f"Expected name '{expected.get('name', '')}', found '{current_name or ''}'"
        )
    if current_email != expected.get("email"):
        errors.append(
            f"Expected email '{expected.get('email', '')}', found '{current_email or ''}'"
        )

    if errors:
        sys.stderr.write("Git identity mismatch.\n")
        for error in errors:
            sys.stderr.write(f"  - {error}\n")
        sys.stderr.write(
            "Update your git config or rerun 'python3 scripts/dev-register.py'.\n"
        )
        return 1

    print("Git identity verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
