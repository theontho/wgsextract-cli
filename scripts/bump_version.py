#!/usr/bin/env python3
"""Set the CLI's version everywhere the release workflow checks it.

Usage: python scripts/bump_version.py 0.4.4

Updates pyproject.toml, pixi.toml and src/wgsextract_cli/__init__.py, the three
files release.yml compares with the vX.Y.Z tag (see docs/releasing.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# File, and the line that holds the version (the first match only).
VERSIONS = [
    ("pyproject.toml", r'^version = "([^"]+)"$'),
    ("pixi.toml", r'^version = "([^"]+)"$'),
    ("src/wgsextract_cli/__init__.py", r'^__version__ = "([^"]+)"$'),
]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not re.fullmatch(r"\d+\.\d+\.\d+", argv[1]):
        print(__doc__, file=sys.stderr)
        return 2
    new = argv[1]
    # Prepare every change before writing any, so a failure changes nothing.
    changes: dict[Path, str] = {}
    old = set()
    for name, pattern in VERSIONS:
        path = ROOT / name
        text = path.read_text()
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            print(f"{name} has no version line; update it by hand.", file=sys.stderr)
            return 1
        old.add(match[1])
        start, end = match.span(1)
        changes[path] = text[:start] + new + text[end:]
    for path, text in changes.items():
        path.write_text(text)
    print(f"Version {new} (was {', '.join(sorted(old))}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
