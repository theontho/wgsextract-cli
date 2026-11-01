"""Restore manifest-declared direct URL digests dropped by Pixi lock refreshes."""

from __future__ import annotations

import argparse
import hashlib
import json
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


def lock_key(uri: str, root: Path) -> str:
    if urlsplit(uri).scheme:
        return urlsplit(uri)._replace(fragment="").geturl()
    return (root / uri).resolve().as_uri()


def restore_hashes(
    manifest: Path,
    lock: Path,
    *,
    require_all: bool = False,
    path_pins: dict[str, str] | None = None,
) -> int:
    digests: dict[str, str] = {}
    for url, digest in declared_digests(tomllib.loads(manifest.read_text())).items():
        base = lock_key(url, manifest.parent)
        if base in digests and digests[base] != digest:
            raise ValueError(f"Conflicting declared digests for {base}")
        digests[base] = digest
    for path, digest in (path_pins or {}).items():
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid declared SHA256 for {path}")
        payload = manifest.parent / path
        payload_hash = hashlib.sha256()
        with payload.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                payload_hash.update(chunk)
        if payload_hash.hexdigest() != digest:
            raise ValueError(f"Local archive checksum disagrees with pin for {path}")
        digests[lock_key(path, manifest.parent)] = digest
    found: set[str] = set()
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
            base = lock_key(uri, manifest.parent)
            expected = digests.get(base)
            block = lines[index:end]
            if expected is not None:
                found.add(base)
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
    if require_all and digests.keys() != found:
        raise ValueError(
            f"Declared URL pins missing from lock: {digests.keys() - found}"
        )
    if restored:
        lock.write_text("\n".join(output) + "\n")
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("pixi.toml"))
    parser.add_argument("--lock", type=Path, default=Path("pixi.lock"))
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument("--path-pins", type=Path)
    args = parser.parse_args()
    print(
        f"Restored {restore_hashes(args.manifest, args.lock, require_all=args.require_all, path_pins=json.loads(args.path_pins.read_text()) if args.path_pins else None)} declared SHA256 pin(s)."
    )


if __name__ == "__main__":
    main()
