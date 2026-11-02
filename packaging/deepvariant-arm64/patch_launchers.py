"""Patch the pinned build scaffold without changing scientific executables."""

from pathlib import Path


def patch_release_script(source: str) -> str:
    extractor = 'os.system("unzip -qq " + zip_path + " -d " + dest_dir)'
    if source.count(extractor) != 1:
        raise ValueError(
            "Pinned Bazel launcher extractor changed; audit before building"
        )
    source = source.replace(
        extractor,
        '__import__("subprocess").run('
        '["unzip", "-qq", zip_path, "-d", dest_dir], check=True)',
    )
    source = source.replace("/tmp/", "/opt/deepvariant/build-tmp/")
    return source.replace(
        "TMPDIR=$(mktemp -d -t tmp.XXXXXXXXXXX)",
        'TMPDIR="/opt/deepvariant/build-tmp/zip-$(printf %s "$orig_zip_file" | tr / -)"; '
        'rm -rf "$TMPDIR"; mkdir -p "$TMPDIR"',
    )


if __name__ == "__main__":
    path = Path("scripts/build/build_release_binaries_arm64.sh")
    path.write_text(patch_release_script(path.read_text()))
