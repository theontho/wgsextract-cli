"""Verify FastQC report statistics and reject FAST5 before starting Java."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fastq = args.output / "arm236.fastq"
    sequence = "ACGT" * 37 + "AC"
    fastq.write_text(
        "".join(f"@arm236_{i}\n{sequence}\n+\n{'I' * 150}\n" for i in range(100)),
        encoding="ascii",
        newline="\n",
    )
    command = [
        sys.executable,
        str(args.launcher),
        "--java",
        str(args.java),
    ]
    completed = subprocess.run(
        [
            *command,
            "--memory",
            "256",
            "--quiet",
            "--extract",
            "--outdir",
            str(args.output),
            str(fastq),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (args.output / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    data = (args.output / "arm236_fastqc/fastqc_data.txt").read_text(encoding="utf-8")
    if "Total Sequences\t100\n" not in data or "Sequence length\t150\n" not in data:
        raise RuntimeError("Native FastQC did not preserve the known read statistics")
    blocked_java = args.output / "must-not-launch-java.exe"
    blocked_java.write_text(
        "This is deliberately not an executable.\n", encoding="ascii"
    )
    blocked = subprocess.run(
        [
            sys.executable,
            str(args.launcher),
            "--java",
            str(blocked_java),
            "--fast5",
            str(fastq),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if blocked.returncode == 0 or "HDF5 JNI is not qualified" not in blocked.stderr:
        raise RuntimeError("FastQC FAST5 did not fail before starting Java")
    (args.output / "result.json").write_text(
        json.dumps({"reads": 100, "length": 150, "fast5_failed_before_java": True})
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
