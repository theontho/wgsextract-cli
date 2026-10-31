"""Reject native payloads hidden inside otherwise architecture-neutral jars."""

import argparse
import io
import json
from pathlib import Path
from zipfile import ZipFile


def native_members(archive: ZipFile, prefix: str = "") -> list[str]:
    found: list[str] = []
    for member in archive.infolist():
        if member.is_dir():
            continue
        name = prefix + member.filename
        with archive.open(member) as stream:
            magic = stream.read(4)
        mach_o = magic in (
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xbe\xba\xfe\xca",
        ) or (magic == b"\xca\xfe\xba\xbe" and not name.endswith(".class"))
        if (
            member.filename.lower().endswith(
                (".dll", ".exe", ".pyd", ".so", ".dylib", ".jnilib")
            )
            or magic[:2] == b"MZ"
            or magic == b"\x7fELF"
            or mach_o
        ):
            found.append(name)
        elif member.filename.lower().endswith((".jar", ".zip")):
            with ZipFile(io.BytesIO(archive.read(member))) as nested:
                found.extend(native_members(nested, name + "!"))
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.root.rglob("*.jar")):
        with ZipFile(path) as archive:
            native = native_members(archive)
        rows.append({"jar": str(path), "native_members": native})
    if not rows:
        raise RuntimeError("No Java archives found for qualification")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    if any(row["native_members"] for row in rows):
        raise RuntimeError("Java archives contain unqualified native libraries")


if __name__ == "__main__":
    main()
