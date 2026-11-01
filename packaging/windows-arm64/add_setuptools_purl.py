"""Declare native conda setuptools' PyPI identity without changing its payload."""

import argparse
import io
import json
import tarfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import zstandard


def add_purl(source: Path, destination: Path) -> None:
    with ZipFile(source) as archive:
        entries = {
            item.filename: (item, archive.read(item)) for item in archive.infolist()
        }
    info = [
        name
        for name in entries
        if name.startswith("info-") and name.endswith(".tar.zst")
    ]
    if len(info) != 1:
        raise ValueError("Expected exactly one conda information archive")
    name = info[0]
    metadata = zstandard.ZstdDecompressor().decompress(
        entries[name][1], max_output_size=16 * 1024**2
    )
    changed = io.BytesIO()
    found = False
    with (
        tarfile.open(fileobj=io.BytesIO(metadata)) as incoming,
        tarfile.open(fileobj=changed, mode="w") as outgoing,
    ):
        for member in incoming:
            stream = incoming.extractfile(member) if member.isfile() else None
            data = stream.read() if stream is not None else b""
            if member.name == "info/index.json":
                index = json.loads(data)
                if (index["name"], index["version"], index["subdir"]) != (
                    "setuptools",
                    "84.2.1",
                    "win-arm64",
                ):
                    raise ValueError(
                        "Only the qualified native setuptools is permitted"
                    )
                index["purls"] = ["pkg:pypi/setuptools"]
                data = (json.dumps(index, indent=2) + "\n").encode()
                member.size = len(data)
                found = True
            outgoing.addfile(member, io.BytesIO(data) if member.isfile() else None)
    if not found:
        raise ValueError("Missing conda index metadata")
    compressed = zstandard.ZstdCompressor(level=19).compress(changed.getvalue())
    entries[name] = (entries[name][0], compressed)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_STORED) as archive:
        for descriptor, payload in entries.values():
            archive.writestr(descriptor, payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    add_purl(args.source, args.destination)


if __name__ == "__main__":
    main()
