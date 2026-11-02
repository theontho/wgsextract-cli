#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare upstream revisions and reproducibly package Yleaf's data release."""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/wgsextract_cli/ydna/yleaf_native"


def git(checkout: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(checkout), *args])


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sync(
    checkout: Path, tag: str, output: Path, *, write_manifest: bool = False
) -> None:
    base = json.loads((PACKAGE / "provenance.json").read_text())
    commit = git(checkout, "rev-parse", f"{tag}^{{commit}}").decode().strip()
    print(git(checkout, "diff", base["commit"], commit, "--", *base["files"]).decode())
    print(
        git(
            checkout, "diff", "--stat", base["commit"], commit, "--", "yleaf/data"
        ).decode()
    )
    output.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "upstream": {**base, "tag": tag, "commit": commit},
        "release_tag": f"yleaf-data-{tag}",
    }
    archives = {}
    for folder in ("hg19", "hg38", "t2t", "hg_prediction_tables"):
        prefix = f"yleaf/data/{folder}"
        paths = (
            git(checkout, "ls-tree", "-r", "--name-only", commit, "--", prefix)
            .decode()
            .splitlines()
        )
        name = f"yleaf-{tag}-{folder}.tar.gz"
        files = {}
        with (output / name).open("wb") as stream:
            with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as gz:
                with tarfile.open(fileobj=gz, mode="w") as archive:
                    for path in sorted(paths):
                        data = git(checkout, "show", f"{commit}:{path}")
                        relative = path.removeprefix("yleaf/data/")
                        info = tarfile.TarInfo(relative)
                        info.size = len(data)
                        info.mode = 0o644
                        archive.addfile(info, io.BytesIO(data))
                        files[relative] = {"sha256": sha256(data), "size": len(data)}
        asset = output / name
        archives[folder] = {
            "url": "https://github.com/theontho/wgsextract-cli/releases/download/"
            f"yleaf-data-{tag}/{name}",
            "sha256": sha256(asset.read_bytes()),
            "size": asset.stat().st_size,
            "files": files,
        }
    manifest["archives"] = archives
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (output / "manifest.json").write_text(content, encoding="utf-8")
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{sha256(path.read_bytes())}  {path.name}\n"
            for path in sorted(output.iterdir())
            if path.name.endswith(".tar.gz") or path.name == "manifest.json"
        ),
        encoding="utf-8",
    )
    (output / "LICENSE.txt").write_bytes(git(checkout, "show", f"{commit}:LICENSE.txt"))
    if write_manifest:
        (PACKAGE / "data_manifest.json").write_text(content, encoding="utf-8")
        base.update(tag=tag, commit=commit)
        (PACKAGE / "provenance.json").write_text(
            json.dumps(base, indent=2) + "\n", encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream", type=Path, required=True, help="Local Yleaf checkout"
    )
    parser.add_argument(
        "--tag", required=True, help="Fetched upstream tag to compare/package"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="out/ data release directory"
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="After reviewing/porting the diff, update packaged pins and provenance",
    )
    args = parser.parse_args()
    sync(args.upstream, args.tag, args.output, write_manifest=args.write_manifest)


if __name__ == "__main__":
    main()
