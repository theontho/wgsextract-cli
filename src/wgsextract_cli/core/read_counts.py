"""Bounded FASTQ sampling for alignment resource estimates, not validation."""

import logging
import math
import stat
import subprocess
import zlib
from pathlib import Path
from typing import NamedTuple, cast

from wgsextract_cli.core.paths import PathInput

_COMPRESSED_SAMPLE_BYTES = 4 * 1024**2
_DECODED_SAMPLE_BYTES = 16 * 1024**2
_CHUNK_BYTES = 64 * 1024


class FastqStatistics(NamedTuple):
    reads: int
    read_length: float | None


class AlignmentSample(NamedTuple):
    read_length: float | None
    max_record_bytes: int | None


_AUX_FIXED_BYTES = {
    b"A": 1,
    b"c": 1,
    b"C": 1,
    b"s": 2,
    b"S": 2,
    b"i": 4,
    b"I": 4,
    b"f": 4,
}


def _bam_record_bytes(fields: list[bytes]) -> int:
    """Upper bound on a SAM line's BAM variable-length data (htslib l_data)."""
    sequence = 0 if fields[9] == b"*" else len(fields[9])
    cigar_ops = sum(fields[5].count(op) for op in b"MIDNSHP=X")
    aux = 0
    for tag in fields[11:]:
        kind, value = tag[3:4], tag[5:].rstrip(b"\r\n")
        if kind in _AUX_FIXED_BYTES:
            aux += 3 + _AUX_FIXED_BYTES[kind]
        elif kind == b"B":
            aux += 8 + 4 * value.count(b",")
        else:
            aux += 4 + len(value)
    # htslib pads the NUL-terminated name to align the CIGAR on 4 bytes.
    qname = (len(fields[0]) + 4) & ~3
    return qname + 4 * cigar_ops + (sequence + 1) // 2 + sequence + aux


def alignment_sample(
    path: PathInput,
    *,
    cram_opt: list[str] | None = None,
) -> AlignmentSample:
    """Read at most 4096 SAM records / 16 MiB: the mean paired primary read
    length and the largest BAM record data among all sampled records."""
    from wgsextract_cli.core.process_registry import proc_registry
    from wgsextract_cli.core.utils import popen

    command = [
        "samtools",
        "view",
        *(cram_opt or []),
        str(path),
    ]
    process = None
    try:
        process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert process.stdout is not None
        scanned = records = bases = consumed = largest = 0
        while scanned < 4096 and consumed < _DECODED_SAMPLE_BYTES:
            line = process.stdout.readline(_DECODED_SAMPLE_BYTES - consumed)
            if not line or not line.endswith(b"\n"):
                break
            consumed += len(line)
            scanned += 1
            fields = line.split(b"\t")
            if len(fields) < 11:
                continue
            largest = max(largest, _bam_record_bytes(fields))
            if int(fields[1]) & 0x901 == 1 and fields[9] != b"*":
                bases += len(fields[9])
                records += 1
        return AlignmentSample(bases / records if records else None, largest or None)
    except (OSError, subprocess.SubprocessError, ValueError):
        logging.debug("Could not sample alignment records: %s", path, exc_info=True)
        return AlignmentSample(None, None)
    finally:
        if process is not None:
            if process.stdout:
                process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            proc_registry.unregister_process(
                " ".join(cast(list[str], process.args)), process
            )


def alignment_read_length(
    path: PathInput,
    *,
    cram_opt: list[str] | None = None,
) -> float | None:
    """Read at most 4096 SAM records / 16 MiB, sampling paired primary lengths."""
    return alignment_sample(path, cram_opt=cram_opt).read_length


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
