"""Build a genotype-free, deterministic AADR asset from public .snp files.

Usage: python scripts/microarray_aadr_markers.py 1240K.snp HO.snp OUTPUT.tsv.gz
The two pinned inputs and their hashes are documented in the templates README.
"""

import argparse
import gzip
from pathlib import Path


def build_markers(panel_1240k: Path, panel_ho: Path, output: Path) -> int:
    ho = {row[0]: row for line in panel_ho.open() if (row := line.split())}
    if not ho:
        raise ValueError("Empty HO panel")
    seen: set[str] = set()
    rows: list[str] = []
    previous = (0, 0)
    with panel_1240k.open() as source:
        for line in source:
            row = line.split()
            if len(row) != 6:
                raise ValueError("Expected six-column EIGENSTRAT SNP metadata")
            snp_id, chrom, genetic, pos, ref, alt = row
            coordinate = (int(chrom), int(pos))
            if (
                snp_id in seen
                or not snp_id.isascii()
                or coordinate <= previous
                or not 1 <= int(chrom) <= 24
                or int(pos) < 1
                or ref not in "ACGT"
                or alt not in "ACGT"
                or len(ref) != 1
                or len(alt) != 1
                or ref == alt
            ):
                raise ValueError(f"Invalid SNP metadata: {snp_id}")
            float(genetic)
            if snp_id in ho and ho[snp_id] != row:
                raise ValueError(f"Panels disagree: {snp_id}")
            rows.append("\t".join([*row, str(int(snp_id in ho))]) + "\n")
            previous = coordinate
            seen.add(snp_id)
    if ho.keys() - seen:
        raise ValueError("HO is not a subset of 1240K")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as target:
        with gzip.GzipFile(
            filename="", fileobj=target, mode="wb", mtime=0
        ) as compressed:
            compressed.write(b"# AADR v66.p1 GRCh37: ID CHROM MORGAN POS REF ALT HO\n")
            compressed.write("".join(rows).encode("ascii"))
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("panel_1240k", type=Path)
    parser.add_argument("panel_ho", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build_markers(args.panel_1240k, args.panel_ho, args.output))


if __name__ == "__main__":
    main()
