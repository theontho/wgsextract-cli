#!/usr/bin/env python3
"""Validate static site source files used by the site generator."""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "site_src"
FRONT_MATTER_DELIMITER = "---"
KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def main() -> None:
    errors: list[str] = []
    if not SOURCE_DIR.is_dir():
        errors.append(f"Missing site source directory: {SOURCE_DIR.relative_to(ROOT)}")
    else:
        for path in sorted(SOURCE_DIR.glob("*.md")):
            errors.extend(validate_markdown_front_matter(path))
        errors.extend(validate_abbr_toml(SOURCE_DIR / "abbr.toml"))

    if errors:
        print("Site source validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        raise SystemExit(1)

    print("Site source validation passed.")


def validate_markdown_front_matter(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    relative = path.relative_to(ROOT)
    errors: list[str] = []

    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        return [f"{relative}:1: Markdown source must start with YAML front matter"]

    try:
        end_index = next(
            index
            for index, line in enumerate(lines[1:], start=2)
            if line.strip() == FRONT_MATTER_DELIMITER
        )
    except StopIteration:
        return [f"{relative}: missing closing front matter delimiter"]

    for line_number, line in enumerate(lines[1 : end_index - 1], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        errors.extend(validate_front_matter_line(relative, line_number, line))

    return errors


def validate_front_matter_line(
    relative: Path, line_number: int, line: str
) -> list[str]:
    errors: list[str] = []
    if line[0].isspace():
        errors.append(
            f"{relative}:{line_number}: site front matter must use simple "
            "top-level key/value lines"
        )

    key, separator, raw_value = line.partition(":")
    if not separator:
        errors.append(f"{relative}:{line_number}: front matter line is missing ':'")
        return errors

    key = key.strip()
    if not KEY_PATTERN.fullmatch(key):
        errors.append(f"{relative}:{line_number}: invalid front matter key {key!r}")

    value = raw_value.strip()
    if value:
        errors.extend(validate_yaml_scalar(relative, line_number, value))

    return errors


def validate_yaml_scalar(relative: Path, line_number: int, value: str) -> list[str]:
    if value.startswith("'"):
        return validate_single_quoted_scalar(relative, line_number, value)
    if value.startswith('"'):
        return validate_double_quoted_scalar(relative, line_number, value)
    if ": " in value or value.endswith(":"):
        return [
            f"{relative}:{line_number}: quote front matter values containing ': ' "
            "so YAML parsers do not treat them as nested mappings"
        ]
    return []


def validate_single_quoted_scalar(
    relative: Path, line_number: int, value: str
) -> list[str]:
    if len(value) < 2 or not value.endswith("'"):
        return [f"{relative}:{line_number}: unterminated single-quoted YAML value"]
    inner = value[1:-1]
    if "'" in inner.replace("''", ""):
        return [
            f"{relative}:{line_number}: single quotes inside YAML single-quoted "
            "values must be doubled"
        ]
    return []


def validate_double_quoted_scalar(
    relative: Path, line_number: int, value: str
) -> list[str]:
    if len(value) < 2 or not value.endswith('"'):
        return [f"{relative}:{line_number}: unterminated double-quoted YAML value"]
    return []


def validate_abbr_toml(path: Path) -> list[str]:
    if not path.exists():
        return [f"{path.relative_to(ROOT)}: missing abbreviation/link TOML file"]
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [f"{path.relative_to(ROOT)}:{error.lineno}: invalid TOML: {error.msg}"]
    return []


if __name__ == "__main__":
    main()
