"""Qualify real native fastp gzip input/output without changing passing reads."""

import argparse
import gzip
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


def run(fastp: Path, output: Path) -> None:
    if sys.platform == "win32":
        if windows_native_machine() != IMAGE_FILE_MACHINE_ARM64:
            raise RuntimeError("Windows fastp qualification requires native ARM64")
        assert_arm64_pe(fastp)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(236)
    reads = b"".join(
        (
            f"@read{index}\n"
            + "".join(rng.choice("ACGT") for _ in range(150))
            + "\n+\n"
            + "I" * 150
            + "\n"
        ).encode("ascii")
        for index in range(2000)
    )
    source, result = output / "input.fq.gz", output / "output.fq.gz"
    source.write_bytes(gzip.compress(reads, mtime=0))
    with (output / "fastp.log").open("wb") as log:
        subprocess.run(
            [
                str(fastp),
                "--in1",
                str(source),
                "--out1",
                str(result),
                "--thread",
                "2",
                "--disable_adapter_trimming",
                "--disable_quality_filtering",
                "--disable_length_filtering",
                "--dont_eval_duplication",
                "--json",
                str(output / "fastp.json"),
                "--html",
                str(output / "fastp.html"),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=120,
        )
    if gzip.decompress(result.read_bytes()) != reads:
        raise RuntimeError("Native fastp gzip round-trip changed passing reads")
    report = json.loads((output / "fastp.json").read_text(encoding="utf-8"))
    for stage in ("before_filtering", "after_filtering"):
        stats = report["summary"][stage]
        if stats["total_reads"] != 2000 or stats["total_bases"] != 300000:
            raise RuntimeError(f"Native fastp counters differ from truth: {stats}")
    print("PASS: 2000 reads / 300000 bases; exact decoded gzip input/output")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastp", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.fastp.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
