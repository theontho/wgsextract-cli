"""Build the reference bootstrap tarball without its YBrowse SNP VCFs (#509).

Derives ``wgsextract-reference-bootstrap-<version>.tar.gz`` from the pinned
``v0.1.0`` bootstrap. It drops the CC-BY-SA YBrowse ``snps_*.vcf.gz`` files
(and their indexes) and the macOS Finder metadata, and points the bundled
README at ``wgsextract ref ybrowse --install``. Everything else is copied
unchanged. The output is byte-reproducible, and a SHA256SUMS file is written
beside it; pin the printed checksum as ``BOOTSTRAP_SHA256``.

    python scripts/build_reference_bootstrap.py \
        --source wgsextract-reference-bootstrap.tar.gz --output out/bootstrap
"""

import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

SOURCE_SHA256 = "5f179e6ab38c7932775c176df38e9d501bac3d41265fe76cc5d5a61b6ae67dc9"
VERSION = "0.2.0"
README = "reference/00README.txt"
YBROWSE = re.compile(r"snps_[^/]*\.vcf\.gz(\.[a-z]+)*")
YBROWSE_SECTION = re.compile(
    r"Y Chromosome SNPs file \(yBrowse\.org\):\n.*?\n\n(?=Exome BED)", re.DOTALL
)
YBROWSE_NOTE = """\
Y Chromosome SNPs file (yBrowse.org):
-- Not included since reference bootstrap v{version}: YBrowse data is
   CC-BY-SA and is not redistributed with WGS Extract. Install it from
   ybrowse.org with 'wgsextract ref ybrowse --install'; it is then used by
   'wgsextract extract ydna-vcf --ybrowse'. The snps_*.vcf.gz files left by
   an older bootstrap are no longer used and can be deleted.

"""


def dropped(name: str) -> bool:
    """Whether the bootstrap member ``name`` is left out."""
    path = PurePosixPath(name)
    return (
        bool(YBROWSE.fullmatch(path.name))
        or path.name.startswith("._")
        or path.name == ".DS_Store"
        or "__MACOSX" in path.parts
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(source: Path, output: Path, version: str, source_sha256: str) -> Path:
    actual = sha256(source)
    if actual != source_sha256:
        raise SystemExit(f"{source}: SHA-256 {actual}, expected {source_sha256}")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"wgsextract-reference-bootstrap-{version}.tar.gz"
    removed = []
    with tarfile.open(source, "r:gz") as old:
        members = sorted(old.getmembers(), key=lambda m: m.name)
        with (
            target.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed,
            tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as new,
        ):
            for member in members:
                if dropped(member.name):
                    removed.append(member.name)
                    continue
                if not (member.isfile() or member.isdir()):
                    raise SystemExit(f"Unexpected bootstrap member: {member.name}")
                info = tarfile.TarInfo(member.name)
                info.type, info.mode, info.mtime = (
                    member.type,
                    member.mode,
                    member.mtime,
                )
                data = old.extractfile(member) if member.isfile() else None
                if member.name == README and data:
                    # The README has CRLF line endings; keep them.
                    text, count = YBROWSE_SECTION.subn(
                        YBROWSE_NOTE.format(version=version),
                        data.read().decode("utf-8").replace("\r\n", "\n"),
                    )
                    text = text.replace("\n", "\r\n")
                    if count != 1:
                        raise SystemExit(f"{README}: no yBrowse section to replace")
                    data = io.BytesIO(text.encode("utf-8"))
                    info.size = len(text.encode("utf-8"))
                else:
                    info.size = member.size
                new.addfile(info, data)
    digest = sha256(target)
    (output / "SHA256SUMS").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    for name in removed:
        print(f"removed {name}")
    print(f"Wrote {target} ({target.stat().st_size} bytes, sha256 {digest})")
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", default=SOURCE_SHA256)
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    build(args.source, args.output, args.version, args.source_sha256)


if __name__ == "__main__":
    main(sys.argv[1:])
