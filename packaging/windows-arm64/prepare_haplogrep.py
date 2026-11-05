"""Remove foreign native resources from the Haplogrep 3 and haplocheck jars.

Both bundle JBWA's JNI kernels (FASTA alignment, which is not qualified), and
Haplogrep 3 also bundles snappy-java's JNI libraries, none of them Windows
ARM64. VCF classification and haplocheck's VCF/BAM path run without them.
"""

import argparse
from pathlib import Path
from zipfile import ZipFile

from audit_java_archives import native_members

# Pinned jar name -> (native member count, prefixes of the removed members).
FOREIGN = {
    "haplogrep3.jar": (33, ("jbwa.zip", "org/xerial/snappy/native/")),
    "haplocheck.jar": (13, ("jbwa.zip",)),
    "mutserve.jar": (0, ()),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count, prefixes = FOREIGN[args.source.name]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.source) as source:
        native = native_members(source)
        if len(native) != count or not all(m.startswith(prefixes) for m in native):
            raise RuntimeError(f"Pinned {args.source.name} native resource set changed")
        if any(
            name.startswith("META-INF/") and name.endswith((".SF", ".RSA", ".DSA"))
            for name in source.namelist()
        ):
            raise RuntimeError(f"Cannot modify a signed {args.source.name} archive")
        with ZipFile(args.output, "w") as output:
            for member in source.infolist():
                if not (prefixes and member.filename.startswith(prefixes)):
                    output.writestr(member, source.read(member))
    with ZipFile(args.output) as output:
        if native_members(output):
            raise RuntimeError(f"Restricted {args.output.name} still has natives")


if __name__ == "__main__":
    main()
