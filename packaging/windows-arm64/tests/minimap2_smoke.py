"""Truth-check native short-read mapping, index round-trip and threading."""

import argparse
import hashlib
import json
import random
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimap2", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(236)
    reference = "".join(rng.choice("ACGT") for _ in range(8000))
    fasta = args.output / "reference.fa"
    fasta.write_text(f">chr236\n{reference}\n", encoding="ascii", newline="\n")
    reads = args.output / "reads.fq"
    positions = {f"read{i:03}": i * 17 + 1 for i in range(300)}
    reads.write_text(
        "".join(
            f"@{name}\n{reference[start - 1 : start + 149]}\n+\n{'I' * 150}\n"
            for name, start in positions.items()
        ),
        encoding="ascii",
        newline="\n",
    )
    index = args.output / "reference.mmi"
    subprocess.run(
        [args.minimap2, "-x", "sr", "-d", str(index), str(fasta)], check=True
    )
    bodies = []
    for threads, target in ((1, fasta), (4, index)):
        sam = args.output / f"t{threads}.sam"
        with sam.open("wb") as output:
            subprocess.run(
                [
                    args.minimap2,
                    "-a",
                    "-x",
                    "sr",
                    "-t",
                    str(threads),
                    str(target),
                    str(reads),
                ],
                stdout=output,
                check=True,
            )
        body = b"".join(
            line
            for line in sam.read_bytes().splitlines(keepends=True)
            if not line.startswith(b"@")
        ).decode("ascii")
        records = body.splitlines()
        if len(records) != len(positions):
            raise RuntimeError(
                f"Expected {len(positions)} mappings, got {len(records)}"
            )
        for record in records:
            fields = record.split("\t")
            name, flag, chromosome, position, _, cigar = fields[:6]
            if (
                flag != "0"
                or chromosome != "chr236"
                or int(position) != positions[name]
                or cigar != "150M"
                or "NM:i:0" not in fields[11:]
            ):
                raise RuntimeError(f"Incorrect synthetic alignment: {record}")
        bodies.append(body)
    if bodies[0] != bodies[1]:
        raise RuntimeError(
            "Mapping differs after native index round-trip/thread change"
        )
    digest = hashlib.sha256(bodies[0].encode("ascii")).hexdigest()
    if args.expected_sha256 and digest != args.expected_sha256:
        raise RuntimeError(f"Native SAM body differs from the Linux golden: {digest}")
    (args.output / "result.json").write_text(
        json.dumps(
            {
                "records": len(positions),
                "sam_sha256": digest,
                "threads_and_index_exact": True,
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )


if __name__ == "__main__":
    main()
