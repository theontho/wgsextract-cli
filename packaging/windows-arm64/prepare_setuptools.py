"""Retain only native ARM64 setuptools launchers and consistent wheel records."""

import argparse
import csv
import shutil
from pathlib import Path

from wgsextract_cli.core.windows_arm64 import pe_machine

FOREIGN = (
    "cli.exe",
    "cli-32.exe",
    "cli-64.exe",
    "gui.exe",
    "gui-32.exe",
    "gui-64.exe",
)


def prepare(source: Path, prefix: Path) -> None:
    for name in FOREIGN:
        if pe_machine(source / "setuptools" / name) not in (0x014C, 0x8664):
            raise ValueError("Pinned setuptools foreign launcher inventory changed")
    for name in ("cli-arm64.exe", "gui-arm64.exe"):
        if pe_machine(source / "setuptools" / name) != 0xAA64:
            raise ValueError("Setuptools ARM64 launcher is not native")
    destination = prefix / "Lib" / "site-packages"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("setuptools", "_distutils_hack", "setuptools-84.2.1.dist-info"):
        shutil.copytree(source / name, destination / name)
    shutil.copyfile(
        source / "distutils-precedence.pth", destination / "distutils-precedence.pth"
    )
    for name in FOREIGN:
        (destination / "setuptools" / name).unlink()
    record = destination / "setuptools-84.2.1.dist-info" / "RECORD"
    removed = {f"setuptools/{name}" for name in FOREIGN}
    with record.open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.reader(stream) if row[0] not in removed]
    with record.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.prefix)


if __name__ == "__main__":
    main()
