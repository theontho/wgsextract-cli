"""Restore manifest-declared direct URL digests dropped by Pixi lock refreshes."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def declared_digests(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str):
            fragment = parse_qs(urlsplit(url).fragment).get("sha256", [])
            digest = value.get("sha256") or (fragment[0] if fragment else None)
            if digest is not None:
                if not isinstance(digest, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", digest
                ):
                    raise ValueError(f"Invalid declared SHA256 for {url}")
                result[url] = digest
        for child in value.values():
            result.update(declared_digests(child))
    elif isinstance(value, list):
        for child in value:
            result.update(declared_digests(child))
    return result


def restore_hashes(manifest: Path, lock: Path) -> int:
    digests = declared_digests(tomllib.loads(manifest.read_text()))
    lines = lock.read_text().splitlines()
    output: list[str] = []
    restored = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        end = index + 1
        if line.startswith(("- conda: ", "- pypi: ")):
            while end < len(lines) and not lines[end].startswith("- "):
                end += 1
            uri = line.split(": ", 1)[1].strip("'\"").removeprefix("direct+")
            expected = digests.get(uri)
            block = lines[index:end]
            if expected is not None:
                actual = next(
                    (
                        item.removeprefix("  sha256: ")
                        for item in block
                        if item.startswith("  sha256: ")
                    ),
                    None,
                )
                if actual is not None and actual != expected:
                    raise ValueError(f"Lock digest disagrees with manifest for {uri}")
                if actual is None:
                    position = 1
                    for offset, item in enumerate(block[1:], 1):
                        if item.startswith(("  name: ", "  version: ")):
                            position = offset + 1
                    block.insert(position, f"  sha256: {expected}")
                    restored += 1
            output.extend(block)
        else:
            output.append(line)
        index = end
    if restored:
        lock.write_text("\n".join(output) + "\n")
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("pixi.toml"))
    parser.add_argument("--lock", type=Path, default=Path("pixi.lock"))
    args = parser.parse_args()
    print(
        f"Restored {restore_hashes(args.manifest, args.lock)} declared SHA256 pin(s)."
    )


if __name__ == "__main__":
    main()
