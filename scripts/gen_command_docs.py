#!/usr/bin/env python3
"""Write docs/commands.md from the command tiers and the help catalog.

Run with --check to fail (exit 1) when the file is out of date.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wgsextract_cli.cli.help import render_markdown
from wgsextract_cli.cli.parser import build_parser

DOC = Path(__file__).resolve().parents[1] / "docs" / "commands.md"


def main() -> int:
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--check", action="store_true", help="only check")
    args = options.parse_args()
    text = render_markdown(build_parser()[0])
    if args.check:
        if not DOC.exists() or DOC.read_text(encoding="utf-8") != text:
            print(f"{DOC} is stale; run 'pixi run gen-command-docs'.", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(text, encoding="utf-8")
    print(f"Wrote {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
