"""Actual sort/index/CRAM/calling qualification on a generated variant fixture."""

import argparse
import hashlib
import json
import random
import subprocess
import sys
from pathlib import Path

from wgsextract_cli.core.windows_arm64 import (
    IMAGE_FILE_MACHINE_ARM64,
    assert_arm64_pe,
    windows_native_machine,
)


def run(samtools: Path, bcftools: Path, output: Path) -> None:
    if sys.platform == "win32":
        if windows_native_machine() != IMAGE_FILE_MACHINE_ARM64:
            raise RuntimeError("Windows pipeline qualification requires native ARM64")
        for executable in (samtools, bcftools):
            assert_arm64_pe(executable)
    output.mkdir(parents=True, exist_ok=True)

    def command(executable: Path, *args: str) -> bytes:
        completed = subprocess.run(
            [str(executable), *args], capture_output=True, check=True, timeout=120
        )
        with (output / "commands.log").open("ab") as log:
            log.write(
                (json.dumps([str(executable), *args]) + "\n").encode()
                + completed.stderr
            )
        return completed.stdout

    rng = random.Random(236)
    reference = "".join(rng.choice("ACGT") for _ in range(4000))
    alternate = next(base for base in "ACGT" if base != reference[500])
    read = reference[450:500] + alternate + reference[501:600]
    fasta = output / "reference.fa"
    fasta.write_text(">chr1\n" + reference + "\n", encoding="ascii", newline="\n")
    sam = output / "input.sam"
    with sam.open("w", encoding="ascii", newline="\n") as stream:
        stream.write(
            "@HD\tVN:1.6\tSO:unsorted\n@SQ\tSN:chr1\tLN:4000\n"
            "@RG\tID:fixture\tSM:arm236\n"
        )
        for index in reversed(range(40)):
            stream.write(
                f"read{index}\t0\tchr1\t451\t60\t150M\t*\t0\t0\t"
                f"{read}\t{'I' * 150}\tRG:Z:fixture\n"
            )
    bam, cram = output / "sorted.bam", output / "sorted.cram"
    command(samtools, "faidx", str(fasta))
    command(samtools, "sort", "-m", "32M", "-o", str(bam), str(sam))
    command(samtools, "index", str(bam))
    command(samtools, "view", "-C", "-T", str(fasta), "-o", str(cram), str(bam))
    command(samtools, "index", str(cram))
    command(samtools, "quickcheck", str(bam), str(cram))
    original = command(samtools, "view", str(bam))
    decoded = command(
        samtools,
        "view",
        "--input-fmt-option",
        "decode_md=0",
        "-T",
        str(fasta),
        str(cram),
    )
    if original != decoded or len(decoded.splitlines()) != 40:
        raise RuntimeError("Decoded CRAM records do not exactly match sorted BAM")
    if command(samtools, "view", "-c", str(cram), "chr1:501-501").strip() != b"40":
        raise RuntimeError("CRAM indexed region lookup lost records")
    depth = command(samtools, "depth", "-r", "chr1:501-501", str(bam))
    if depth.strip() != b"chr1\t501\t40":
        raise RuntimeError(f"Native coverage differs from fixture truth: {depth!r}")
    pileup, vcf = output / "pileup.bcf", output / "calls.vcf"
    command(
        bcftools,
        "mpileup",
        "-f",
        str(fasta),
        "-B",
        "-q",
        "20",
        "-Q",
        "20",
        "-d",
        "1000",
        "-Ob",
        "-o",
        str(pileup),
        str(bam),
    )
    command(bcftools, "call", "-m", "-v", "-Ov", "-o", str(vcf), str(pileup))
    records = command(
        bcftools, "query", "-f", "%CHROM\\t%POS\\t%REF\\t%ALT[\\t%GT]\\n", str(vcf)
    )
    expected = f"chr1\t501\t{reference[500]}\t{alternate}\t1/1\n".encode("ascii")
    if records != expected:
        raise RuntimeError(f"Called variant differs from truth: {records!r}")
    result = {
        "records": 40,
        "decoded_sam_sha256": hashlib.sha256(decoded).hexdigest(),
        "variant": records.decode("ascii").strip(),
        "vcf_body_sha256": hashlib.sha256(
            b"".join(
                line
                for line in vcf.read_bytes().splitlines(keepends=True)
                if not line.startswith(b"#")
            )
        ).hexdigest(),
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samtools", required=True, type=Path)
    parser.add_argument("--bcftools", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.samtools.resolve(), args.bcftools.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
