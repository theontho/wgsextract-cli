#!/usr/bin/env python3
"""Point the WGSExtract bundle at another wgsextract-cli release.

Usage: scripts/bump-wgsextract-cli.py v0.4.4

Updates the release tag setup installs, the bundle version that names the
installers, the tests and docs that pin them, and adds a CHANGELOG entry.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG_FILE = ROOT / "examples/WGSExtract/scripts/wgsextract-release-tag.txt"
# Files that name the bundled CLI release's version.
PINNED = [
    "docs/distribution.md",
    "examples/WGSExtract/manifest.json",
    "app/tests/conformance.test.mjs",
    "tools/packaging/tests/test_embedded_branding.py",
]
CHANGELOG = ROOT / "docs/CHANGELOG.md"
RELEASES = "https://github.com/WGSExtract/wgsextract-cli/releases/tag"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not re.fullmatch(r"v\d+\.\d+\.\d+", argv[1]):
        print(__doc__, file=sys.stderr)
        return 2
    new_tag = argv[1]
    old_tag = TAG_FILE.read_text().strip()
    if new_tag == old_tag:
        print(f"The bundle already uses {new_tag}.")
        return 0
    old, new = old_tag[1:], new_tag[1:]
    # Prepare every change before writing any, so a failure leaves the tree as it
    # was; the tag file goes last, since a rerun compares against it.
    changes: dict[Path, str] = {}
    # The version alone, not inside another (10.4.3, 0.4.30).
    version = re.compile(rf"(?<![\d.]){re.escape(old)}(?!\.?\d)")
    for name in PINNED:
        path = ROOT / name
        text, count = version.subn(new, path.read_text())
        if not count:
            print(f"{name} doesn't mention {old}; update it by hand.", file=sys.stderr)
            return 1
        changes[path] = text
    changes[CHANGELOG] = with_changelog_entry(
        CHANGELOG.read_text(),
        f"- Bump the bundled WGSExtract CLI installer target to `{new_tag}` "
        f"([release notes]({RELEASES}/{new_tag})).",
    )
    changes[TAG_FILE] = new_tag + "\n"
    for path, text in changes.items():
        path.write_text(text)
    print(f"Bundle now uses wgsextract-cli {new_tag} (was {old_tag}).")
    return 0


def with_changelog_entry(text: str, entry: str) -> str:
    """Add entry first under ## Unreleased / ### Changed, making either if missing
    (a release moves Unreleased's sections under its own dated heading)."""
    heading = "## Unreleased\n"
    if heading not in text:
        first = text.find("\n## ")
        at = first + 1 if first >= 0 else len(text)
        text = text[:at] + heading + "\n" + text[at:]
    start = text.index(heading) + len(heading)
    end = text.find("\n## ", start)
    end = end + 1 if end >= 0 else len(text)
    changed = text.find("### Changed\n", start, end)
    if changed >= 0:
        at = changed + len("### Changed\n")
        return text[:at] + entry + "\n" + text[at:]
    return text[:start] + "\n### Changed\n" + entry + "\n" + text[start:]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
