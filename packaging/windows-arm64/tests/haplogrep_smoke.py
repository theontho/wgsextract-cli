"""Compare real Haplogrep 3 / haplocheck runs before and after removing the
foreign JNI resources from their jars."""

import argparse
import csv
import json
import subprocess
from pathlib import Path

# Two rCRS differences almost every sample has: haplogroup H2a2a.
HEADER = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chrM,length=16569>\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    '##FORMAT=<ID=AF,Number=A,Type=Float,Description="Allele level">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tarm236\n"
)
VARIANTS = ((73, "A", "G"), (263, "A", "G"))


def run(java: Path, jar: Path, workdir: Path, args: list[str]) -> str:
    completed = subprocess.run(
        [
            str(java),
            "-Xmx512m",
            "-XX:-UsePerfData",
            f"-Djava.io.tmpdir={workdir.resolve()}",
            "-jar",
            str(jar.resolve()),
            *args,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return completed.stdout + completed.stderr


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--restricted", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tree", type=Path, help="Haplogrep 3 tree directory")
    args = parser.parse_args()
    haplogrep = args.original.name == "haplogrep3.jar"
    if haplogrep and not args.tree:
        parser.error("--tree is required for haplogrep3.jar")
    body = "".join(
        f"chrM\t{pos}\t.\t{ref}\t{alt}\t.\tPASS\t.\tGT:AF\t1:1.0\n"
        for pos, ref, alt in VARIANTS
    )
    report = {}
    for label, jar in (("original", args.original), ("restricted", args.restricted)):
        work = args.output / label
        work.mkdir(parents=True, exist_ok=True)
        (work / "arm236.vcf").write_text(HEADER + body, encoding="ascii", newline="\n")
        if haplogrep:
            tree_yaml = (args.tree / "tree.yaml").resolve().as_posix()
            (work / "haplogrep3.yaml").write_text(
                f"repositories: []\nphylotrees:\n  - {json.dumps(tree_yaml)}\n",
                encoding="utf-8",
            )
            log = run(
                args.java,
                jar,
                work,
                [
                    "classify",
                    "--in",
                    "arm236.vcf",
                    "--out",
                    "result.txt",
                    "--tree",
                    "phylotree-fu-rcrs@1.3",
                ],
            )
        else:
            log = run(
                args.java, jar, work, ["--raw", "--out", "result.txt", "arm236.vcf"]
            )
        (args.output / f"{label}.log").write_text(log, encoding="utf-8")
        report[label] = (work / "result.txt").read_bytes()
    if report["original"] != report["restricted"]:
        raise RuntimeError(
            f"Removing native resources changed {args.original.name} output"
        )
    rows = list(
        csv.DictReader(report["restricted"].decode().splitlines(), delimiter="\t")
    )
    column = "Haplogroup" if haplogrep else "Contamination Status"
    value = rows[0][column].strip('"') if len(rows) == 1 else None
    expected = "H2a2a" if haplogrep else "ND"
    if value != expected:
        raise RuntimeError(
            f"{args.original.name} gave {column} {value!r}, not {expected}"
        )
    (args.output / "result.json").write_text(
        json.dumps({"jar": args.original.name, "byte_exact": True, column: value})
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
