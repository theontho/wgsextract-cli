#!/usr/bin/env python3
"""Pin pixi.toml's Windows tool packages to a published Windows Tools release.

    python packaging/windows/pin.py windows-tools-2
    pixi lock

Rewrites the block between `# windows-tools-begin` and `# windows-tools-end`
with each .conda asset's URL and the SHA-256 digest GitHub reports for it.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = "theontho/wgsextract-cli"
MANIFEST = Path(__file__).resolve().parents[2] / "pixi.toml"
BLOCK = re.compile(r"(# windows-tools-begin[^\n]*\n).*?(# windows-tools-end)", re.S)
# <name>-<version>-<build>.conda; names may contain dashes (bwa-mem2).
ASSET = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+)-(?P<build>[^-]+)\.conda$")


def release_assets(tag: str) -> list[dict[str, str]]:
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return list(json.load(response)["assets"])


def pin_lines(assets: list[dict[str, str]]) -> list[str]:
    lines = []
    for asset in sorted(assets, key=lambda a: a["name"]):
        match = ASSET.match(asset["name"])
        if not match:
            continue
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            raise SystemExit(f"{asset['name']}: GitHub reports no SHA-256 digest")
        lines.append(
            f'{match["name"]} = {{ url = "{asset["browser_download_url"]}", '
            f'sha256 = "{digest.removeprefix("sha256:")}" }}'
        )
    if not lines:
        raise SystemExit("The release has no .conda packages.")
    return lines


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    lines = pin_lines(release_assets(sys.argv[1]))
    text = MANIFEST.read_text(encoding="utf-8")
    if not BLOCK.search(text):
        raise SystemExit(f"{MANIFEST} has no windows-tools-begin/end block.")
    text = BLOCK.sub(lambda m: m[1] + "\n".join(lines) + "\n" + m[2], text, count=1)
    MANIFEST.write_text(text, encoding="utf-8", newline="\n")
    print("\n".join(lines))
    print("Now run `pixi lock`.")


if __name__ == "__main__":
    main()
