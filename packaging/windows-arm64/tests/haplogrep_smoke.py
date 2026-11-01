"""Compare real VCF classification before and after removing foreign JBWA."""

import argparse
import csv
import json
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--restricted", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    vcf = args.output / "arm236.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=MT,length=16569>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tarm236\n"
        "MT\t73\t.\tA\tG\t.\tPASS\t.\tGT\t1\n"
        "MT\t263\t.\tA\tG\t.\tPASS\t.\tGT\t1\n",
        encoding="ascii",
        newline="\n",
    )
    outputs = []
    for label, jar in (
        ("original", args.original),
        ("restricted", args.restricted),
    ):
        output = args.output / f"{label}.txt"
        completed = subprocess.run(
            [
                str(args.java),
                "-Xmx256m",
                "-XX:-UsePerfData",
                f"-Djava.io.tmpdir={args.output.resolve()}",
                "-jar",
                str(jar),
                "classify",
                "--in",
                str(vcf),
                "--format",
                "vcf",
                "--out",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        (args.output / f"{label}.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
        outputs.append(output.read_bytes())
    if outputs[0] != outputs[1]:
        raise RuntimeError("Removing JBWA changed VCF classification bytes")
    rows = list(csv.DictReader(outputs[1].decode("utf-8").splitlines(), delimiter="\t"))
    if len(rows) != 1 or rows[0]["Haplogroup"] != "H2a2a":
        raise RuntimeError("Haplogrep did not classify the known mitochondrial fixture")
    (args.output / "result.json").write_text(
        json.dumps({"classification_byte_exact": True, "haplogroup": "H2a2a"}) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
