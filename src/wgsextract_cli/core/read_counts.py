"""Bounded FASTQ sampling for alignment resource estimates, not validation."""

import logging
import math
import stat
import zlib
from pathlib import Path
from typing import NamedTuple

from wgsextract_cli.core.paths import PathInput

_COMPRESSED_SAMPLE_BYTES = 4 * 1024**2
_DECODED_SAMPLE_BYTES = 16 * 1024**2
_CHUNK_BYTES = 64 * 1024


class FastqStatistics(NamedTuple):
    reads: int
    read_length: float | None


def fastq_statistics(path: PathInput) -> FastqStatistics | None:
    """Estimate individual reads from file size and a bounded prefix sample.

    Plain FASTQ, gzip and concatenated gzip/BGZF members are supported. Small
    files are counted exactly. Measure bytes actually consumed by the inflater,
    not its read-ahead, which otherwise biases highly compressed inputs.
    """
    try:
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular FASTQ file")
        size = info.st_size
        with open(path, "rb") as source:
            compressed = source.read(2) == b"\x1f\x8b"
            source.seek(0)
            if not compressed:
                data = source.read(_DECODED_SAMPLE_BYTES)
                consumed = len(data)
                exact = consumed == size
            else:
                decoded = bytearray()
                inflater = zlib.decompressobj(31)
                pending = b""
                consumed = 0
                exact = False
                while len(decoded) < _DECODED_SAMPLE_BYTES:
                    if not pending:
                        remaining = _COMPRESSED_SAMPLE_BYTES - source.tell()
                        if remaining <= 0:
                            break
                        pending = source.read(min(_CHUNK_BYTES, remaining))
                        if not pending:
                            raise EOFError("incomplete gzip member")
                    decoded.extend(
                        inflater.decompress(
                            pending, _DECODED_SAMPLE_BYTES - len(decoded)
                        )
                    )
                    tail = inflater.unused_data or inflater.unconsumed_tail
                    consumed += len(pending) - len(tail)
                    pending = tail
                    if inflater.eof:
                        if not pending and source.tell() == size:
                            exact = True
                            break
                        inflater = zlib.decompressobj(31)
                data = bytes(decoded)
        lines = data.splitlines()
        if not exact and not data.endswith(b"\n"):
            lines = lines[:-1]
        records = len(lines) // 4
        if not records or (exact and len(lines) % 4):
            raise ValueError("no complete FASTQ sample")
        for offset in range(0, records * 4, 4):
            name, sequence, plus, quality = lines[offset : offset + 4]
            if (
                not name.startswith(b"@")
                or not plus.startswith(b"+")
                or len(sequence) != len(quality)
            ):
                raise ValueError("not a four-line FASTQ sample")
        reads = records if exact else math.ceil(size * records / consumed)
        read_length = (
            sum(len(lines[offset]) for offset in range(1, records * 4, 4)) / records
        )
    except (OSError, EOFError, ValueError, zlib.error) as error:
        logging.warning("Could not estimate input read count from %s: %s", path, error)
        return None
    logging.info(
        "%s input read count for %s: %s (%s sampled bytes).",
        "Exact" if exact else "Estimated",
        path,
        f"{reads:,}",
        f"{consumed:,}",
    )
    return FastqStatistics(reads, read_length or None)


def fastq_read_count(path: PathInput) -> int | None:
    """Estimate individual reads using the bounded FASTQ prefix sample."""
    statistics = fastq_statistics(path)
    return statistics.reads if statistics is not None else None


def fastq_input_statistics(
    r1: PathInput, r2: PathInput | None = None
) -> FastqStatistics | None:
    """Count both mates and their read-weighted sampled mean length."""
    samples = [fastq_statistics(path) for path in (r1, r2) if path is not None]
    if any(sample is None for sample in samples):
        return None
    valid = [sample for sample in samples if sample is not None]
    reads = sum(sample.reads for sample in valid)
    length = (
        sum(sample.reads * (sample.read_length or 0) for sample in valid) / reads
        if reads and all(sample.read_length is not None for sample in valid)
        else None
    )
    return FastqStatistics(reads, length)


def fastq_input_reads(r1: PathInput, r2: PathInput | None = None) -> int | None:
    """Total individual reads, not pairs; do not omit an unreadable mate."""
    statistics = fastq_input_statistics(r1, r2)
    return statistics.reads if statistics is not None else None
