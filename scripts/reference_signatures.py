#!/usr/bin/env python3
"""Compute the sequence-dictionary signature of reference catalog genomes.

The signature is what `calculate_bam_md5` gives the header of a BAM aligned to
the genome. It goes in the catalog's Signature column
(src/wgsextract_cli/assets/reference/seed_genomes.csv), and the genome's
signature needs a REFERENCE_MODELS entry so `info` can identify it exactly.

Uses a published .fai next to the download when there is one; otherwise
streams the FASTA (gzip or plain) through a length counter without keeping it,
or for a .zip, downloads it to a temporary file first.

    pixi run python scripts/reference_signatures.py hs38d1s T2Tv20
    pixi run python scripts/reference_signatures.py --all
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wgsextract_cli.core.reference_assets import sequence_signature

CATALOG = (
    Path(__file__).resolve().parents[1]
    / "src/wgsextract_cli/assets/reference/seed_genomes.csv"
)
# Prints name<TAB>length per record, like a .fai's first two columns.
LENGTHS_AWK = (
    '/^>/{if(n!="")print n"\\t"l; n=substr($1,2); l=0; next}'
    '{l+=length($0)} END{if(n!="")print n"\\t"l}'
)


def published_fai(url: str) -> dict[str, int] | None:
    for candidate in (url + ".fai", url.removesuffix(".gz") + ".fai"):
        try:
            with urllib.request.urlopen(candidate, timeout=60) as resp:
                rows = [line.split("\t") for line in resp.read().decode().splitlines()]
        except (OSError, UnicodeDecodeError):
            continue
        if rows and all(len(r) >= 2 and r[1].isdigit() for r in rows):
            return {r[0]: int(r[1]) for r in rows}
    return None


def streamed_lengths(url: str) -> dict[str, int]:
    count = f"LC_ALL=C awk '{LENGTHS_AWK}'"
    with tempfile.TemporaryDirectory() as tmp:
        if url.endswith(".zip"):
            archive = Path(tmp) / "genome.zip"
            subprocess.run(
                ["curl", "-sfL", "--retry", "5", "-o", str(archive), url], check=True
            )
            pipeline = f"unzip -p '{archive}' | {count}"
        else:
            decode = "gzip -dc" if url.endswith((".gz", ".bgz")) else "cat"
            pipeline = f"curl -sfL --retry 5 '{url}' | {decode} | {count}"
        out = subprocess.run(
            ["bash", "-c", f"set -o pipefail; {pipeline}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    rows = (line.split("\t") for line in out.splitlines())
    return {name: int(length) for name, length in rows}


def signature(row: dict[str, str]) -> str:
    lengths = published_fai(row["URL"]) or streamed_lengths(row["URL"])
    return f"{sequence_signature(lengths)}\t{len(lengths)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("codes", nargs="*", help="catalog codes (Pyth Code column)")
    parser.add_argument("--all", action="store_true", help="every catalog genome")
    args = parser.parse_args()
    if not args.codes and not args.all:
        parser.error("name catalog codes, or pass --all")

    with CATALOG.open(encoding="utf-8") as f:
        rows = [
            r for r in csv.DictReader(f) if args.all or r["Pyth Code"] in args.codes
        ]
    unique = list({r["Final File Name"]: r for r in rows}.values())
    print("code\tfile\tsignature\tcontigs")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for row, result in zip(unique, pool.map(signature, unique), strict=True):
            print(f"{row['Pyth Code']}\t{row['Final File Name']}\t{result}", flush=True)


if __name__ == "__main__":
    main()
