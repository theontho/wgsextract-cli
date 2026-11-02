"""Disposable, checksummed columnar copies of verified site-map assets."""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
from array import array
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wgsextract_cli.core.microarray_site_map import SiteMap, _Metadata

_HEADER = struct.Struct("<8sII")
_MAGIC = b"WGSCOL\x01\x00"
_COLUMNS = (
    ("_source_codes", "B"),
    ("_source_positions", "I"),
    ("_target_codes", "B"),
    ("_target_positions", "I"),
    ("_flags", "B"),
    ("_native_order", "I"),
)


def cache_path(directory: Path, manifest: bytes) -> Path:
    key = hashlib.sha256(manifest).hexdigest()
    return directory / f"sites-v1-{sys.byteorder}-{key}.bin"


def read(path: Path, metadata: _Metadata) -> SiteMap | None:
    from wgsextract_cli.core.microarray_site_map import SiteMap

    rows = sum(contig.rows for contig in metadata.source)
    maximum = _HEADER.size + 15 * rows
    try:
        with path.open("rb") as source:
            data = source.read(maximum + 33)
        if not _HEADER.size + 32 <= len(data) <= maximum + 32:
            return None
        payload, digest = data[:-32], data[-32:]
        if hashlib.sha256(payload).digest() != digest:
            return None
        magic, cached_rows, native = _HEADER.unpack_from(payload)
        if magic != _MAGIC or cached_rows != rows or native > rows:
            return None
        if len(payload) != _HEADER.size + 11 * rows + 4 * native:
            return None
        offset = _HEADER.size
        columns = []
        for name, code in _COLUMNS:
            column: array[int] = array(code)
            size = (native if name == "_native_order" else rows) * column.itemsize
            column.frombytes(memoryview(payload)[offset : offset + size])
            columns.append(column)
            offset += size
        spans = {}
        offset = 0
        for contig in metadata.source:
            spans[contig.name] = (offset, offset + contig.rows)
            offset += contig.rows
        return SiteMap(
            tuple(contig.name for contig in metadata.source),
            tuple(contig.name for contig in metadata.target),
            MappingProxyType(spans),
            *columns,
        )
    except (OSError, ValueError, struct.error):
        return None


def write(path: Path, mapping: SiteMap) -> None:
    payload = bytearray(
        _HEADER.pack(_MAGIC, len(mapping._source_positions), len(mapping._native_order))
    )
    for name, _code in _COLUMNS:
        payload.extend(getattr(mapping, name).tobytes())
    digest = hashlib.sha256(payload).digest()
    with tempfile.TemporaryDirectory(dir=path.parent, prefix="sites-") as work:
        staged = Path(work, path.name)
        with staged.open("wb") as destination:
            destination.write(payload)
            destination.write(digest)
        os.replace(staged, path)
