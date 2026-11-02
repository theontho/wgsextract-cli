"""Cache exact reverse-chain projections, never inferred forward-chain inverses."""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import NamedTuple

from pyliftover import LiftOver

from wgsextract_cli.core.file_lock import build_lock

# Codes double as the output sort order, matching legacy chr_to_int(): MT before X/Y.
_CHROMS = (*(f"chr{i}" for i in range(1, 23)), "chrM", "chrX", "chrY")
_CODES = {chrom: code for code, chrom in enumerate(_CHROMS)}
_MAGIC = b"WGSHG19\x02"
_HEADER = struct.Struct("<8sIII")
_ROW = struct.Struct("<IBQB")


class Projection(NamedTuple):
    source_index: int
    chrom: str
    position: int
    inverted: bool


def _read(path: Path, count: int) -> tuple[Iterator[Projection], int, int] | None:
    try:
        with path.open("rb") as source:
            data = source.read(_HEADER.size + count * _ROW.size + 33)
        if len(data) < _HEADER.size + 32:
            return None
        payload, digest = data[:-32], data[-32:]
        if hashlib.sha256(payload).digest() != digest:
            return None
        magic, rows, bad_chrom, bad_pos = _HEADER.unpack_from(payload)
        if (
            magic != _MAGIC
            or rows + bad_chrom + bad_pos > count
            or len(payload) != _HEADER.size + rows * _ROW.size
        ):
            return None
        return (
            (
                Projection(index, _CHROMS[code], position, bool(inverted))
                for index, code, position, inverted in _ROW.iter_unpack(
                    memoryview(payload)[_HEADER.size :]
                )
            ),
            bad_chrom,
            bad_pos,
        )
    except (OSError, ValueError, struct.error):
        return None


def _write(path: Path, rows: list[Projection], bad_chrom: int, bad_pos: int) -> None:
    payload = bytearray(_HEADER.pack(_MAGIC, len(rows), bad_chrom, bad_pos))
    for row in rows:
        payload.extend(
            _ROW.pack(row.source_index, _CODES[row.chrom], row.position, row.inverted)
        )
    digest = hashlib.sha256(payload).digest()
    with tempfile.TemporaryDirectory(dir=path.parent, prefix="coordinates-") as work:
        staged = Path(work, path.name)
        with staged.open("wb") as destination:
            destination.write(payload)
            destination.write(digest)
        os.replace(staged, path)


def _project(
    coordinates: Sequence[tuple[str, int]], chain: str
) -> tuple[list[Projection], int, int]:
    lifter = LiftOver(chain)
    rows = []
    bad_chrom = bad_pos = 0
    for index, (chrom, position) in enumerate(coordinates):
        try:
            hits = lifter.convert_coordinate(chrom, position - 1)
        except (ValueError, TypeError):
            continue
        if not hits:
            bad_pos += 1
            continue
        hit = hits[0]
        if hit[0] not in _CODES:
            bad_chrom += 1
            continue
        rows.append(Projection(index, hit[0], hit[1] + 1, hit[2:3] == ("-",)))
    rows.sort(key=lambda row: (_CODES[row.chrom], row.position))
    return rows, bad_chrom, bad_pos


def project_coordinates(
    coordinates: Sequence[tuple[str, int]],
    chain: str,
    library: str | None = None,
) -> tuple[Iterator[Projection], int, int]:
    if not library:
        rows, bad_chrom, bad_pos = _project(coordinates, chain)
        return iter(rows), bad_chrom, bad_pos
    chain_digest = hashlib.sha256()
    with Path(chain).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            chain_digest.update(chunk)
    digest = hashlib.sha256(_MAGIC + chain_digest.digest())
    for chrom, position in coordinates:
        digest.update(f"{chrom}\t{position}\n".encode())
    directory = Path(library, "microarray", "hg19-coordinates")
    path = directory / f"{digest.hexdigest()}.bin"
    cached = _read(path, len(coordinates))
    if cached is not None:
        return cached
    with ExitStack() as stack:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stack.enter_context(build_lock(str(path) + ".lock"))
        except OSError as error:
            logging.warning("Microarray coordinate cache unavailable: %s", error)
            rows, bad_chrom, bad_pos = _project(coordinates, chain)
            return iter(rows), bad_chrom, bad_pos
        cached = _read(path, len(coordinates))
        if cached is not None:
            return cached
        rows, bad_chrom, bad_pos = _project(coordinates, chain)
        try:
            _write(path, rows, bad_chrom, bad_pos)
        except OSError as error:
            logging.warning("Could not save microarray coordinate cache: %s", error)
    return iter(rows), bad_chrom, bad_pos
