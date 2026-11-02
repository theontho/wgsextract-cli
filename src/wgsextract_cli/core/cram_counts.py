"""Exact CRAM record totals without decoding alignment records.

CRAM 3 container headers store *records*, not mapped reads. In particular,
placed-unmapped records belong to the same container as mapped records. These
totals must never be substituted for idxstats' mapped/unmapped columns.
"""

import io
import os
import struct
import zlib
from collections import Counter
from typing import BinaryIO

from .paths import PathInput


def _read(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Truncated CRAM header")
    return data


def _itf8(stream: BinaryIO) -> int:
    first = _read(stream, 1)[0]
    if first < 0x80:
        value = first
    elif first < 0xC0:
        value = ((first & 0x3F) << 8) | _read(stream, 1)[0]
    elif first < 0xE0:
        value = ((first & 0x1F) << 16) | int.from_bytes(_read(stream, 2), "big")
    elif first < 0xF0:
        value = ((first & 0x0F) << 24) | int.from_bytes(_read(stream, 3), "big")
    else:
        data = _read(stream, 4)
        value = (
            ((first & 0x0F) << 28)
            | (data[0] << 20)
            | (data[1] << 12)
            | (data[2] << 4)
            | (data[3] & 0x0F)
        )
    return value - (1 << 32) if value & (1 << 31) else value


def _ltf8(stream: BinaryIO) -> int:
    first = _read(stream, 1)[0]
    size = 0
    while size < 8 and first & (0x80 >> size):
        size += 1
    value = first & (0xFF >> (size + 1)) if size < 8 else 0
    return (value << (8 * size)) | int.from_bytes(_read(stream, size), "big")


def cram_read_counts(path: PathInput) -> dict[int, int] | None:
    """Return exact per-reference record totals, or None to request idxstats.

    Only CRAM 3.0/3.1 with single-reference containers and an intact header
    chain/EOF marker is supported. No reference or index is needed. The caller
    must retain idxstats wherever the mapped/unmapped split is required.
    """
    try:
        with open(path, "rb", buffering=0) as raw:
            size = os.fstat(raw.fileno()).st_size
            stream = io.BufferedReader(raw, 256)
            header = _read(stream, 26)
            if header[:4] != b"CRAM" or header[4:6] not in (b"\x03\x00", b"\x03\x01"):
                return None
            counts: Counter[int] = Counter()
            first = True
            while stream.tell() < size:
                # A container header is bounded (and its CRC checked) before any
                # data are trusted; seek past compressed records, never read them.
                start = stream.tell()
                length_bytes = _read(stream, 4)
                length = struct.unpack("<i", length_bytes)[0]
                if length < 0:
                    return None
                ref, pos, span, records = (_itf8(stream) for _ in range(4))
                _ltf8(stream)  # record counter
                _ltf8(stream)  # bases
                blocks = _itf8(stream)
                landmarks = _itf8(stream)
                if (
                    records < 0
                    or span < 0
                    or blocks < 0
                    or landmarks < 0
                    or landmarks > length
                ):
                    return None
                previous = -1
                for _ in range(landmarks):
                    offset = _itf8(stream)
                    if not previous < offset < length:
                        return None
                    previous = offset
                end_header = stream.tell()
                checksum = _read(stream, 4)
                end = stream.tell() + length
                if end > size:
                    return None
                stream.seek(start)
                encoded = _read(stream, end_header - start)
                if zlib.crc32(encoded) != int.from_bytes(checksum, "little"):
                    return None
                if first:
                    if records:
                        return None
                    first = False
                elif ref == -1 and pos == 0x454F46 and records == 0:
                    return dict(counts) if end == size and length == 15 else None
                elif ref < -1:
                    return None
                else:
                    counts[ref] += records
                stream.seek(end)
    except (OSError, ValueError, OverflowError):
        return None
    return None
