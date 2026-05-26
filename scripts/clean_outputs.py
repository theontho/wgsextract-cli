#!/usr/bin/env python3
"""Remove gitignored scratch directories used for build/test output.

Targets `out/` and `tmp/` at the repo root by default. Both are gitignored,
so this only deletes local artifacts. Pass `--dry-run` to preview.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

DEFAULT_TARGETS = ("out", "tmp")


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() or entry.is_symlink():
                total += entry.lstat().st_size
        except OSError:
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help=f"Directories to clean (default: {' '.join(DEFAULT_TARGETS)}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    repo_root_resolved = repo_root.resolve()
    for name in args.targets:
        target = repo_root / name
        if not target.exists() and not target.is_symlink():
            print(f"skip {target} (does not exist)")
            continue
        if target.is_symlink():
            print(f"skip {target} (symlink; refusing to traverse)")
            continue
        if not target.is_dir():
            print(f"skip {target} (not a directory)")
            continue
        try:
            target_resolved = target.resolve(strict=True)
        except OSError as exc:
            print(f"skip {target} ({exc})")
            continue
        if (
            target_resolved != repo_root_resolved
            and repo_root_resolved not in target_resolved.parents
        ):
            print(f"skip {target} (resolves outside repo: {target_resolved})")
            continue
        size = _dir_size(target)
        action = "would remove" if args.dry_run else "removing"
        print(f"{action} {target} ({_human_size(size)})")
        if not args.dry_run:
            shutil.rmtree(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
