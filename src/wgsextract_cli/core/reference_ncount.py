"""N-run statistics for reference FASTAs, written as WGS Extract _ncnt.csv/_nbin.csv.

The file layout matches the sidecars shipped in the reference bootstrap bundle
(produced by WGS Extract v4's countingNs.py): tab-separated despite the .csv
suffix, thousands separators in counts, `#` comment headers.

_ncnt.csv has one row per sequence: name, length, N count, number of N runs
longer than NRUN_SIZE, their mean and sample standard deviation, the number of
shorter runs, the bucket size (length / NUM_BUCKETS), then a sparse list of
(bucket start, round(ln(N count in bucket))) pairs for buckets with a nonzero
value. _nbin.csv lists every run longer than NRUN_SIZE with its 0-based start.
"""

from __future__ import annotations

import gzip
import math
import os
import re
import statistics
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, TextIO, cast

NUM_BUCKETS = 1000
NRUN_SIZE = 300
_READ_SIZE = 16 * 1024 * 1024
_N_RUN_RE = re.compile(rb"[Nn]+")
_SEQUENCE_WHITESPACE = b"\r\n\t "


@dataclass
class NCountTotals:
    sequences: int = 0
    length: int = 0
    n_count: int = 0
    large_runs: int = 0
    small_runs: int = 0


@dataclass
class _Sequence:
    name: str
    length: int = 0
    runs: list[list[int]] = field(default_factory=list)

    def feed(self, bases: bytes) -> None:
        for match in _N_RUN_RE.finditer(bases):
            start, end = self.length + match.start(), self.length + match.end()
            if self.runs and self.runs[-1][1] == start:
                self.runs[-1][1] = end  # run continues across a chunk boundary
            else:
                self.runs.append([start, end])
        self.length += len(bases)


def write_n_count_files(fasta: str, ncnt_path: str, nbin_path: str) -> NCountTotals:
    """Scan a (optionally gzip/bgzip-compressed) FASTA and write both sidecars.

    Outputs are written to temporary names and moved into place only on success.
    """
    name = os.path.basename(fasta)
    totals = NCountTotals()
    ncnt_tmp, nbin_tmp = ncnt_path + ".partial", nbin_path + ".partial"
    try:
        with (
            _open_fasta(fasta) as fasta_file,
            open(ncnt_tmp, "w", encoding="utf-8", newline="\n") as ncnt,
            open(nbin_tmp, "w", encoding="utf-8", newline="\n") as nbin,
        ):
            ncnt.write(
                f"#Processing Ref Model: {name} with >{NRUN_SIZE}bp runs of N and "
                f"{NUM_BUCKETS} buckets per sequence\n"
                "#Seq\tNumBP\tNumNs\tNumNreg\tNregSizeMean\tNregSizeStdDev\t"
                "SmlNreg\tBuckSize\tBucket Sparse List (bp start, ln value) when nonzero\n"
            )
            nbin.write(
                f"#Processing Ref Model: {name} with >{NRUN_SIZE}bp of N runs\n"
                "#SN\tBinID\tStart\tSize\n"
            )
            for sequence in _iter_sequences(fasta_file):
                _write_sequence(sequence, ncnt, nbin, totals)
            ncnt.write(
                "#TOTALS:\n"
                f"{totals.sequences}\t{totals.length:,}\t{totals.n_count:,}\t"
                f"{totals.large_runs}\t\t\t{totals.small_runs}\n"
            )
        os.replace(ncnt_tmp, ncnt_path)
        os.replace(nbin_tmp, nbin_path)
    finally:
        for tmp in (ncnt_tmp, nbin_tmp):
            if os.path.exists(tmp):
                os.remove(tmp)
    return totals


def _open_fasta(fasta: str) -> BinaryIO:
    with open(fasta, "rb") as f:
        is_gzipped = f.read(2) == b"\x1f\x8b"
    if is_gzipped:
        return cast(BinaryIO, gzip.open(fasta, "rb"))
    return open(fasta, "rb")


def _iter_sequences(fasta_file: BinaryIO) -> Iterator[_Sequence]:
    """Yield each FASTA record with its N runs, reading in large chunks."""
    current: _Sequence | None = None
    pending = b""
    while True:
        chunk = fasta_file.read(_READ_SIZE)
        data = pending + chunk
        pending = b""
        last_header = data.rfind(b">")
        if chunk and last_header != -1 and data.find(b"\n", last_header) == -1:
            # Hold back a header line split across reads; sequence lines can be
            # split anywhere because N runs are merged across feeds.
            data, pending = data[:last_header], data[last_header:]
        pos = 0
        while pos < len(data):
            header = data.find(b">", pos)
            body_end = len(data) if header == -1 else header
            if body_end > pos:
                if current is None:
                    if data[pos:body_end].strip():
                        raise ValueError("FASTA sequence data found before a header.")
                else:
                    current.feed(
                        data[pos:body_end].translate(None, _SEQUENCE_WHITESPACE)
                    )
            if header == -1:
                break
            line_end = data.find(b"\n", header)
            line_end = len(data) if line_end == -1 else line_end
            if current is not None:
                yield current
            fields = data[header + 1 : line_end].split()
            if not fields:
                raise ValueError("FASTA header without a sequence name.")
            current = _Sequence(fields[0].decode("utf-8", errors="replace"))
            pos = line_end + 1
        if not chunk:
            break
    if current is not None:
        yield current


def _write_sequence(
    sequence: _Sequence, ncnt: TextIO, nbin: TextIO, totals: NCountTotals
) -> None:
    run_lengths = [end - start for start, end in sequence.runs]
    large = [
        (start, end - start) for start, end in sequence.runs if end - start > NRUN_SIZE
    ]
    large_sizes = [size for _, size in large]
    n_count = sum(run_lengths)
    mean = statistics.fmean(large_sizes) if large_sizes else 0.0
    stdev = statistics.stdev(large_sizes) if len(large_sizes) > 1 else 0.0
    small_runs = len(run_lengths) - len(large)

    for index, (start, size) in enumerate(large, 1):
        nbin.write(f"{sequence.name}\t{index}\t{start:,}\t{size:,}\n")

    bucket_size = max(1, round(sequence.length / NUM_BUCKETS))
    bucket_counts: dict[int, int] = {}
    for start, end in sequence.runs:
        while start < end:
            bucket = start // bucket_size
            bucket_end = min(end, (bucket + 1) * bucket_size)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + bucket_end - start
            start = bucket_end
    sparse = "".join(
        f"\t{bucket * bucket_size}\t{value}"
        for bucket, count in sorted(bucket_counts.items())
        if (value := round(math.log(count))) > 0
    )
    ncnt.write(
        f"{sequence.name}\t{sequence.length:,}\t{n_count:,}\t{len(large)}\t"
        f"{mean:,.0f}\t{stdev:,.0f}\t{small_runs}\t{bucket_size}{sparse}\n"
    )

    totals.sequences += 1
    totals.length += sequence.length
    totals.n_count += n_count
    totals.large_runs += len(large)
    totals.small_runs += small_runs
