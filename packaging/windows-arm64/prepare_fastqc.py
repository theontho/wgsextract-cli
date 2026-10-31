"""Build a restricted FastQC distribution without foreign HDF5 JNI payloads."""

import argparse
import shutil
from pathlib import Path
from zipfile import ZipFile

from audit_java_archives import native_members


def prepare_launcher(source: str) -> str:
    changes = {
        "import subprocess": (
            "import subprocess\nfrom _wgse_windows_arm64 import assert_arm64_pe"
        ),
        "base_dir = Path(__file__).resolve().parent": (
            'base_dir = Path(__file__).resolve().parents[1] / "Library/share/fastqc"'
        ),
        'java_bin = "java"': 'java_bin = str(base_dir.parents[1] / "bin/java.exe")',
        "    java_bin = find_java_bin()": (
            "    if options.fast5 or any(str(f).lower().endswith('.fast5') for f in options.files):\n"
            "        exit_with_error('Native Windows ARM64 FastQC does not support FAST5: HDF5 JNI is not qualified. Use FASTQ.')\n"
            "    java_bin = find_java_bin()\n"
            "    try:\n"
            "        assert_arm64_pe(Path(sys.executable))\n"
            "        assert_arm64_pe(Path(java_bin))\n"
            "    except (OSError, ValueError) as error:\n"
            "        exit_with_error(str(error))"
        ),
        (
            '    if "CLASSPATH" in os.environ:\n'
            '        write_debug("Found existing CLASSPATH containing "+os.environ["CLASSPATH"])\n'
            '        new_elements.insert(0,os.environ["CLASSPATH"])\n'
        ): "",
    }
    for original, replacement in changes.items():
        if source.count(original) != 1:
            raise RuntimeError("Pinned FastQC launcher source changed")
        source = source.replace(original, replacement)
    return source


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    destination = args.prefix / "Library/share/fastqc"
    destination.mkdir(parents=True, exist_ok=True)
    for directory in ("uk", "net", "org", "Configuration", "Help", "Templates"):
        shutil.copytree(
            args.source / directory,
            destination / directory,
            ignore=shutil.ignore_patterns("*.java"),
        )
    for file in ("LICENSE", "LICENSE_JHDF5.txt", "README.md", "RELEASE_NOTES.txt"):
        shutil.copyfile(args.source / file, destination / file)
    for jar in args.source.glob("*.jar"):
        if jar.name != "cisd-jhdf5.jar":
            shutil.copyfile(jar, destination / jar.name)
            continue
        with ZipFile(jar) as original:
            native = set(native_members(original))
            if len(native) != 16 or any(
                not name.startswith("native/") for name in native
            ):
                raise RuntimeError("Pinned FastQC HDF5 native resource set changed")
            if any(
                name.startswith("META-INF/") and name.endswith((".SF", ".RSA", ".DSA"))
                for name in original.namelist()
            ):
                raise RuntimeError("Cannot modify a signed FastQC HDF5 archive")
            with ZipFile(destination / jar.name, "w") as restricted:
                for member in original.infolist():
                    if member.filename not in native:
                        restricted.writestr(member, original.read(member))
    bin_directory = args.prefix / "bin"
    bin_directory.mkdir(parents=True, exist_ok=True)
    (bin_directory / "fastqc").write_text(
        prepare_launcher((args.source / "fastqc").read_text(encoding="utf-8")),
        encoding="utf-8",
        newline="\n",
    )
    shutil.copyfile(
        args.repo / "src/wgsextract_cli/core/windows_arm64.py",
        bin_directory / "_wgse_windows_arm64.py",
    )
    shutil.copyfile(args.repo / "LICENSE", destination / "LICENSE_WGSEXTRACT.txt")


if __name__ == "__main__":
    main()
