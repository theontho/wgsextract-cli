import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli.commands.info.metrics import parse_idxstats
from wgsextract_cli.core.cram_counts import cram_read_counts
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.core.warnings import indexed_read_count


def _itf8(value: int) -> bytes:
    if 0 <= value < 128:
        return bytes([value])
    value &= 0xFFFFFFFF
    return bytes(
        [
            0xF0 | (value >> 28),
            (value >> 20) & 255,
            (value >> 12) & 255,
            (value >> 4) & 255,
            value & 15,
        ]
    )


def _container(ref: int, records: int, pos: int = 1, data: bytes = b"x") -> bytes:
    header = struct.pack("<i", len(data))
    header += b"".join(_itf8(n) for n in (ref, pos, 0, records))
    header += b"\0\0\1\0"  # counters, blocks, landmarks
    return header + struct.pack("<I", zlib.crc32(header)) + data


def _file(*containers: bytes, version: bytes = b"\3\0") -> bytes:
    return (
        b"CRAM"
        + version
        + b"\0" * 20
        + _container(0, 0)
        + b"".join(containers)
        + _container(-1, 0, 0x454F46, b"\0" * 15)
    )


def test_container_totals_and_nocoord(tmp_path: Path):
    path = tmp_path / "reads.cram"
    path.write_bytes(_file(_container(0, 7), _container(1, 3), _container(-1, 2)))
    assert cram_read_counts(path) == {0: 7, 1: 3, -1: 2}
    path.with_suffix(".cram.crai").touch()
    with patch("wgsextract_cli.core.warnings.run_command") as command:
        assert indexed_read_count(path) == 12
    command.assert_not_called()
    path.with_suffix(".cram.crai").unlink()
    assert indexed_read_count(path) is None


@pytest.mark.parametrize(
    "data",
    [
        _file(_container(-2, 5)),
        _file(_container(0, 2), version=b"\2\1"),
        _file(_container(0, 2), version=b"\4\0"),
        _file(_container(0, 2))[:-1],
        _file(_container(0, 2)) + b"x",
        b"not a CRAM",
    ],
)
def test_unsupported_or_truncated_requests_fallback(tmp_path: Path, data: bytes):
    path = tmp_path / "reads.cram"
    path.write_bytes(data)
    assert cram_read_counts(path) is None


def test_header_checksum_requests_fallback(tmp_path: Path):
    path = tmp_path / "reads.cram"
    data = bytearray(_file(_container(0, 2)))
    data[31] ^= 1
    path.write_bytes(data)
    assert cram_read_counts(path) is None


@needs_htslib
@pytest.mark.parametrize("version", ["3.0", "3.1"])
@pytest.mark.parametrize("unmapped_only", [False, True])
def test_real_cram_counts_preserve_placed_unmapped_split(
    tmp_path: Path, version: str, unmapped_only: bool
):
    reference = write_fasta(tmp_path / "ref.fa", {"chr1": "A" * 100})
    reads = [
        "mapped\t0\tchr1\t1\t60\t10M\t*\t0\t0\tAAAAAAAAAA\tIIIIIIIIII",
        "placed\t4\tchr1\t1\t0\t*\t*\t0\t0\tAAAAAAAAAA\tIIIIIIIIII",
    ]
    bam = sam_to_bam(
        [
            "@SQ\tSN:chr1\tLN:100",
            *([] if unmapped_only else reads),
            "unplaced\t4\t*\t0\t0\t*\t*\t0\t0\tAAAAAAAAAA\tIIIIIIIIII",
        ],
        tmp_path / "reads.bam",
    )
    cram = tmp_path / "reads.cram"
    run_command(
        [
            "samtools",
            "view",
            "-C",
            "-T",
            str(reference),
            "--output-fmt-option",
            f"version={version}",
            "--output-fmt-option",
            "multi_seq_per_slice=0",
            "-o",
            str(cram),
            bam,
        ]
    )
    run_command(["samtools", "index", str(cram)])
    rows, _, mapped, unmapped = parse_idxstats(str(cram))
    assert (mapped, unmapped) == ((0, 1) if unmapped_only else (1, 2))
    counts = cram_read_counts(cram)
    assert counts == ({-1: 1} if unmapped_only else {0: 2, -1: 1})
    assert sum(counts.values()) == mapped + unmapped
    assert indexed_read_count(cram) == mapped + unmapped
    assert rows[0]["mapped"] == (0 if unmapped_only else 1)
    assert rows[0]["unmapped"] == (0 if unmapped_only else 1)
