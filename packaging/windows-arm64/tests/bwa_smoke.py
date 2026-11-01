"""Generated paired-read alignment truth and threaded output parity gate."""

import argparse
import hashlib
import random
import subprocess
import sys
from pathlib import Path

from wgsextract_cli.core.windows_arm64 import (
    IMAGE_FILE_MACHINE_ARM64,
    assert_arm64_pe,
    windows_native_machine,
)


def run(bwa: Path, output: Path, expected_sha256: str | None = None) -> None:
    if sys.platform == "win32":
        if windows_native_machine() != IMAGE_FILE_MACHINE_ARM64:
            raise RuntimeError("The Windows qualification gate requires native ARM64")
        assert_arm64_pe(bwa)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(236)
    reference = "".join(rng.choice("ACGT") for _ in range(4000))
    fasta = output / "reference.fa"
    fasta.write_text(">chr1\n" + reference + "\n", encoding="ascii", newline="\n")
    fastqs = [output / "reads.1.fq", output / "reads.2.fq"]
    truth: dict[str, tuple[int, int]] = {}
    complement = str.maketrans("ACGT", "TGCA")
    with (
        fastqs[0].open("w", newline="\n") as first,
        fastqs[1].open("w", newline="\n") as second,
    ):
        for index in range(300):
            start = index * 11
            mate = start + 200
            name = f"read{index}"
            truth[name] = (start + 1, mate + 1)
            forward = reference[start : start + 150]
            reverse = reference[mate : mate + 150].translate(complement)[::-1]
            for stream, read in ((first, forward), (second, reverse)):
                stream.write(f"@{name}\n{read}\n+\n{'I' * 150}\n")
    with (output / "index.log").open("w") as index_log:
        subprocess.run([str(bwa), "index", str(fasta)], stderr=index_log, check=True)
    bodies = []
    for threads in (1, 4):
        sam = output / f"threads-{threads}.sam"
        with (
            sam.open("wb") as sam_stream,
            (output / f"threads-{threads}.log").open("wb") as alignment_log,
        ):
            subprocess.run(
                [
                    str(bwa),
                    "mem",
                    "-K",
                    "100000000",
                    "-t",
                    str(threads),
                    str(fasta),
                    *(str(path) for path in fastqs),
                ],
                stdout=sam_stream,
                stderr=alignment_log,
                check=True,
            )
        body = b"".join(
            line
            for line in sam.read_bytes().splitlines(keepends=True)
            if not line.startswith(b"@")
        )
        records = body.decode("ascii").splitlines()
        if len(records) != len(truth) * 2:
            raise RuntimeError(f"Expected {len(truth) * 2} records, got {len(records)}")
        for record in records:
            fields = record.split("\t")
            flag = int(fields[1])
            expected_position = truth[fields[0]][0 if flag & 64 else 1]
            if (
                fields[2] != "chr1"
                or int(fields[3]) != expected_position
                or fields[5] != "150M"
                or "NM:i:0" not in fields[11:]
            ):
                raise RuntimeError(f"Alignment differs from generated truth: {record}")
        bodies.append(body)
    if bodies[0] != bodies[1]:
        raise RuntimeError("Single-thread and four-thread SAM bodies differ")
    digest = hashlib.sha256(bodies[0]).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise RuntimeError(f"SAM body differs from the Linux oracle: {digest}")
    (output / "body.sha256").write_text(digest + "\n", encoding="ascii")
    print(f"PASS: 600 truth-checked records; t1/t4 exact; SAM body SHA256 {digest}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bwa", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    run(args.bwa.resolve(), args.output.resolve(), args.expected_sha256)


if __name__ == "__main__":
    main()
