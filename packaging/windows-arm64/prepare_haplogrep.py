"""Remove Haplogrep's foreign JBWA archive; FASTA input is not qualified."""

import argparse
from pathlib import Path
from zipfile import ZipFile

from audit_java_archives import native_members


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.source) as source:
        native = native_members(source)
        if len(native) != 13 or any(
            not member.startswith("jbwa.zip!") for member in native
        ):
            raise RuntimeError("Pinned Haplogrep JBWA resource set changed")
        if any(
            name.startswith("META-INF/") and name.endswith((".SF", ".RSA", ".DSA"))
            for name in source.namelist()
        ):
            raise RuntimeError("Cannot modify a signed Haplogrep archive")
        with ZipFile(args.output, "w") as output:
            for member in source.infolist():
                if member.filename != "jbwa.zip":
                    output.writestr(member, source.read(member))


if __name__ == "__main__":
    main()
