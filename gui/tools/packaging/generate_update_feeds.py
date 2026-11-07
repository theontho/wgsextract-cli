#!/usr/bin/env python3
"""Generate GitHub Release-backed update metadata for desktop packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    parser.add_argument("--tag", required=True, help="GitHub release tag")
    parser.add_argument("--version", required=True, help="SemVer app version")
    parser.add_argument("--artifacts-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--release-base-url", default="")
    args = parser.parse_args()

    artifacts = collect_artifacts(args.artifacts_root)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.release_base_url or f"https://github.com/{args.repo}/releases/download/{args.tag}"

    if not write_tauri_latest_json(args.output_dir / "latest.json", artifacts, args.version, base_url):
        print("No updater feed was generated; signed updater artifacts were not found.", file=sys.stderr)
    return 0


def collect_artifacts(root: Path) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    duplicates: dict[str, list[Path]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not is_release_artifact_file(path.name):
            continue
        existing = artifacts.get(path.name)
        if existing is not None:
            duplicates.setdefault(path.name, [existing]).append(path)
            continue
        artifacts[path.name] = path
    if duplicates:
        details = ", ".join(
            f"{name}: {', '.join(str(path.relative_to(root)) for path in paths)}"
            for name, paths in sorted(duplicates.items())
        )
        raise ValueError(f"Duplicate release artifact names are ambiguous: {details}")
    return artifacts


def is_release_artifact_file(name: str) -> bool:
    return (
        name == "latest.json"
        or name.endswith(
            (
                ".sig",
                ".app.tar.gz",
                ".dmg",
                ".AppImage",
                ".deb",
                ".rpm",
                ".pkg.tar.zst",
                "-setup.exe",
                "-quick-uninstall.ps1",
            )
        )
    )


def write_tauri_latest_json(
    output_path: Path,
    artifacts: dict[str, Path],
    version: str,
    base_url: str,
) -> bool:
    platforms: dict[str, dict[str, str]] = {}
    add_tauri_platform(platforms, artifacts, "windows-x86_64", [r".*-setup\.exe$"], base_url)
    add_tauri_platform(platforms, artifacts, "linux-x86_64", [r".*\.AppImage$"], base_url)
    add_tauri_platform(platforms, artifacts, "darwin-aarch64", [r".*_aarch64\.app\.tar\.gz$"], base_url)
    add_tauri_platform(platforms, artifacts, "darwin-x86_64", [r".*_x64\.app\.tar\.gz$"], base_url)

    if not platforms:
        return False

    payload = {
        "version": version,
        "notes": f"GUI for CLI {version}",
        "pub_date": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return True


def add_tauri_platform(
    platforms: dict[str, dict[str, str]],
    artifacts: dict[str, Path],
    platform: str,
    patterns: list[str],
    base_url: str,
) -> None:
    artifact = first_matching_artifact(artifacts, patterns)
    if artifact is None:
        return
    signature_path = artifacts.get(f"{artifact.name}.sig")
    if signature_path is None:
        return
    signature = signature_path.read_text(encoding="utf-8").strip()
    if not signature:
        return
    platforms[platform] = {
        "signature": signature,
        "url": f"{base_url}/{url_escape_path(artifact.name)}",
    }


def first_matching_artifact(artifacts: dict[str, Path], patterns: list[str]) -> Path | None:
    for pattern in patterns:
        regex = re.compile(pattern)
        matches = sorted(path for name, path in artifacts.items() if regex.fullmatch(name))
        if matches:
            return matches[0]
    return None


def url_escape_path(value: str) -> str:
    from urllib.parse import quote

    return quote(release_asset_name(value))


def release_asset_name(value: str) -> str:
    return value.replace(" ", ".")


if __name__ == "__main__":
    raise SystemExit(main())
