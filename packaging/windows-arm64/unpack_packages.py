"""Extract qualified local conda artifacts for build-time integration tests."""

import argparse
import tarfile
from pathlib import Path
from zipfile import ZipFile

import zstandard


def unpack(source: Path, prefix: Path) -> None:
    packages = sorted(source.rglob("*.conda"))
    if not packages:
        raise RuntimeError("No native conda package artifacts were downloaded")
    prefix.mkdir(parents=True, exist_ok=True)
    for package in packages:
        with ZipFile(package) as archive:
            payloads = [
                name
                for name in archive.namelist()
                if name.startswith("pkg-") and name.endswith(".tar.zst")
            ]
            if len(payloads) != 1:
                raise RuntimeError(f"Invalid conda payload count in {package}")
            with (
                archive.open(payloads[0]) as compressed,
                zstandard.ZstdDecompressor().stream_reader(compressed) as reader,
                tarfile.open(fileobj=reader, mode="r|") as payload,
            ):
                payload.extractall(prefix, filter="data")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--prefix", required=True, type=Path)
    args = parser.parse_args()
    unpack(args.source, args.prefix)


if __name__ == "__main__":
    main()
