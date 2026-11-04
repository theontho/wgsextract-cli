"""Nucleus-style region selections (DeepVariant's --regions) as merged intervals."""

import gzip
import re

Intervals = dict[str, list[tuple[int, int]]]


def literal_region(selection: str, lengths: dict[str, int]) -> tuple[str, int, int]:
    if selection in lengths:
        return selection, 0, lengths[selection]
    match = re.match(r"^(\S+):([0-9,]+)-([0-9,]+)$", selection)
    if match:
        chrom, start_text, end_text = match.groups()
        start, end = int(start_text.replace(",", "")), int(end_text.replace(",", ""))
    else:
        match = re.match(r"^(\S+):([0-9,]+)$", selection)
        if not match:
            raise ValueError(f"Invalid interval: {selection}")
        chrom, position = match.groups()
        start = end = int(position.replace(",", ""))
    if start < 1 or end < start:
        raise ValueError(f"Invalid interval: {selection}")
    return chrom, start - 1, end


def _bed_intervals(path: str) -> list[tuple[str, int, int]]:
    lower = path.lower()
    opener = gzip.open if lower.endswith(".gz") else open
    rows = []
    with opener(path, "rt") as source:
        for line in source:
            if not line.strip() or line.startswith(("#", "track", "browser")):
                continue
            fields = line.split()
            try:
                if lower.endswith(".bedpe"):
                    if fields[0] != fields[3]:
                        continue
                    chrom, start, end = fields[0], int(fields[1]), int(fields[5])
                else:
                    chrom, start, end = fields[0], int(fields[1]), int(fields[2])
            except IndexError as error:
                raise ValueError(f"Invalid interval: {line.strip()}") from error
            if start < 0 or end < start:
                raise ValueError(f"Invalid interval: {line.strip()}")
            rows.append((chrom, start, end))
    return rows


def selected_intervals(region: str, lengths: dict[str, int]) -> Intervals:
    """Union of whitespace-separated literals and BED/BED.gz/BEDPE files.

    Rows on contigs absent from ``lengths`` are dropped and intervals are
    clipped to the contig; raises ValueError when nothing remains.
    """
    intervals: Intervals = {}
    for selection in region.split():
        if selection.lower().endswith((".bed", ".bed.gz", ".bedpe")):
            rows = _bed_intervals(selection)
        else:
            rows = [literal_region(selection, lengths)]
        for chrom, start, end in rows:
            if chrom not in lengths:
                continue
            start, end = min(start, lengths[chrom]), min(end, lengths[chrom])
            if end > start:
                intervals.setdefault(chrom, []).append((start, end))
    merged: Intervals = {}
    for chrom, ranges in intervals.items():
        union: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if union and start <= union[-1][1]:
                union[-1] = (union[-1][0], max(union[-1][1], end))
            else:
                union.append((start, end))
        merged[chrom] = union
    if not merged:
        raise ValueError("Selection covers no reference bases.")
    return merged
