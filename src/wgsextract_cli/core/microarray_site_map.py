"""Bounded, bidirectional decoding of the CHM13 microarray site-map asset."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import re
import zlib
from array import array
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from wgsextract_cli.core import microarray_site_map_cache
from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.utils import WGSExtractError

_MAGIC = b"WGSCMAP\x01"
_MAX_COMPRESSED = 16 * 1024 * 1024
_MAX_UNCOMPRESSED = 64 * 1024 * 1024
_MAX_ROWS = 4_000_000
_UINT32_MAX = (1 << 32) - 1
_CHROMS = (*(f"chr{i}" for i in range(1, 23)), "chrX", "chrY", "chrM")
_STATUSES = (
    "mapped",
    "unmapped",
    "multiple",
    "nonprimary",
    "nonreciprocal",
    "collision",
    "mt_gap",
    "unplaced",
)
_REF_STATUSES = ("ref_unknown", "ref_equal", "ref_different")


class SiteProjection(NamedTuple):
    source_chrom: str
    source_pos: int
    target_chrom: str | None
    target_pos: int | None
    inverted: bool
    status: str
    ref_status: str


@dataclass(frozen=True)
class _Contig:
    name: str
    length: int
    rows: int = 0


@dataclass(frozen=True)
class _Metadata:
    filename: str
    compressed_bytes: int
    compressed_sha256: str
    uncompressed_bytes: int
    uncompressed_sha256: str
    source: tuple[_Contig, ...]
    target: tuple[_Contig, ...]


@dataclass(frozen=True)
class SiteMap:
    _source_names: tuple[str, ...]
    _target_names: tuple[str, ...]
    _source_spans: Mapping[str, tuple[int, int]]
    _source_codes: array[int]
    _source_positions: array[int]
    _target_codes: array[int]
    _target_positions: array[int]
    _flags: array[int]
    _native_order: array[int]

    def source_lookup(self, chrom: str, position: int) -> SiteProjection | None:
        span = self._source_spans.get(chrom)
        if span is None:
            return None
        begin, end = span
        index = bisect_left(self._source_positions, position, begin, end)
        if index == end or self._source_positions[index] != position:
            return None
        return self._projection(index)

    def source_sites(self) -> Iterator[SiteProjection]:
        for index in range(len(self._source_positions)):
            yield self._projection(index)

    def native_sites(self) -> Iterator[SiteProjection]:
        for index in self._native_order:
            yield self._projection(index)

    def native_range(
        self, chrom: str, start: int, end: int
    ) -> Iterator[SiteProjection]:
        if not 1 <= start <= end <= _UINT32_MAX:
            return
        try:
            code = self._target_names.index(chrom) + 1
        except ValueError:
            return
        begin = bisect_left(
            self._native_order, (code << 32) | start, key=self._native_key
        )
        finish = bisect_left(
            self._native_order, (code << 32) + end + 1, key=self._native_key
        )
        for offset in range(begin, finish):
            yield self._projection(self._native_order[offset])

    def _native_key(self, index: int) -> int:
        return (self._target_codes[index] << 32) | self._target_positions[index]

    def _projection(self, index: int) -> SiteProjection:
        flags = self._flags[index]
        target_code = self._target_codes[index]
        return SiteProjection(
            self._source_names[self._source_codes[index]],
            self._source_positions[index],
            self._target_names[target_code - 1] if target_code else None,
            self._target_positions[index] if target_code else None,
            bool(flags & 0x10),
            _STATUSES[flags & 0x0F],
            _REF_STATUSES[flags >> 5],
        )


def _integer(value: object, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Expected an integer in [{minimum}, {maximum}]")
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Invalid SHA256 digest")
    return value


def _contigs(value: object, *, source: bool) -> tuple[_Contig, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(_CHROMS):
        raise ValueError("Invalid contig list")
    result: list[_Contig] = []
    previous = -1
    for row in value:
        if not isinstance(row, dict) or row.get("name") not in _CHROMS:
            raise ValueError("Invalid primary chromosome")
        name = row["name"]
        order = _CHROMS.index(name)
        if order <= previous:
            raise ValueError("Duplicate or unsorted chromosomes")
        previous = order
        result.append(
            _Contig(
                name,
                _integer(row.get("length"), _UINT32_MAX, 1),
                _integer(row.get("rows"), _MAX_ROWS) if source else 0,
            )
        )
    return tuple(result)


def _metadata(value: object) -> _Metadata:
    if not isinstance(value, dict):
        raise ValueError("Manifest is not an object")
    if (
        value.get("format") != "wgsextract-chm13-map"
        or type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("source_build") != "GRCh37-rCRS"
        or value.get("target_build") != "CHM13v2.0"
    ):
        raise ValueError("Unsupported site-map schema or builds")
    filename = value.get("file")
    if (
        not isinstance(filename, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.gz", filename) is None
    ):
        raise ValueError("Invalid asset filename")
    source = _contigs(value.get("source_contigs"), source=True)
    if not 1 <= sum(contig.rows for contig in source) <= _MAX_ROWS:
        raise ValueError("Invalid total site count")
    return _Metadata(
        filename,
        _integer(value.get("compressed_bytes"), _MAX_COMPRESSED, 1),
        _sha256(value.get("sha256")),
        _integer(value.get("uncompressed_bytes"), _MAX_UNCOMPRESSED, 1),
        _sha256(value.get("uncompressed_sha256")),
        source,
        _contigs(value.get("target_contigs"), source=False),
    )


class _Reader:
    def __init__(self, stream: gzip.GzipFile, metadata: _Metadata) -> None:
        self.metadata = metadata
        # Bound decompression before decoding, and hash once rather than once
        # per varint byte. The manifest's limit is checked before construction.
        self.data = stream.read(metadata.uncompressed_bytes + 1)
        if len(self.data) > metadata.uncompressed_bytes:
            raise ValueError("Decompressed data exceeds the declared length")
        if len(self.data) != metadata.uncompressed_bytes:
            raise ValueError("Decompressed length mismatch")
        if hashlib.sha256(self.data).hexdigest() != metadata.uncompressed_sha256:
            raise ValueError("Decompressed SHA256 mismatch")
        self.offset = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        data = self.data[self.offset : end]
        self.offset = end
        return data

    def exact(self, size: int) -> bytes:
        data = self.read(size)
        if len(data) != size:
            raise ValueError("Truncated site map")
        return data

    def varint(self) -> int:
        result = 0
        for shift in range(0, 35, 7):
            if self.offset >= len(self.data):
                raise ValueError("Truncated site map")
            byte = self.data[self.offset]
            self.offset += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                if result > _UINT32_MAX or (shift and byte == 0):
                    raise ValueError("Overflow or nonminimal varint")
                return result
        raise ValueError("Overlong varint")

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise ValueError("Trailing site-map records")


def _decode(data: bytes, metadata: _Metadata) -> SiteMap:
    source_codes = array("B")
    source_positions = array("I")
    target_codes = array("B")
    target_positions = array("I")
    flags = array("B")
    native_order = array("I")
    spans = {}
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as stream:
        reader = _Reader(stream, metadata)
        if reader.exact(len(_MAGIC)) != _MAGIC:
            raise ValueError("Wrong site-map magic/version")
        if reader.varint() != len(metadata.source):
            raise ValueError("Chromosome block count mismatch")
        mapped = 0
        for source_code, contig in enumerate(metadata.source):
            if reader.varint() != contig.rows:
                raise ValueError("Chromosome row count mismatch")
            begin = len(source_positions)
            source_pos = offset = 0
            for _ in range(contig.rows):
                delta = reader.varint()
                source_pos += delta
                if not delta or source_pos > contig.length:
                    raise ValueError("Duplicate, unsorted or out-of-range source site")
                flag = reader.exact(1)[0]
                status = flag & 0x0F
                if status >= len(_STATUSES) or flag >> 5 >= len(_REF_STATUSES):
                    raise ValueError("Invalid site status or reserved flags")
                target_code = target_pos = 0
                if status == 0:
                    mapped += 1
                    target_code = reader.varint()
                    if not 1 <= target_code <= len(metadata.target):
                        raise ValueError("Invalid destination chromosome")
                    encoded = reader.varint()
                    offset += -(encoded // 2) - 1 if encoded & 1 else encoded // 2
                    target_pos = source_pos + offset
                    if not 1 <= target_pos <= metadata.target[target_code - 1].length:
                        raise ValueError("Out-of-range destination site")
                elif flag & 0x70:
                    raise ValueError("Unmapped site has orientation or REF flags")
                source_codes.append(source_code)
                source_positions.append(source_pos)
                target_codes.append(target_code)
                target_positions.append(target_pos)
                flags.append(flag)
            spans[contig.name] = (begin, len(source_positions))

        if reader.varint() != mapped:
            raise ValueError("Native index count mismatch")
        previous_index = 0
        previous_key = -1
        for _ in range(mapped):
            encoded = reader.varint()
            index = previous_index + (
                -(encoded // 2) - 1 if encoded & 1 else encoded // 2
            )
            if not 0 <= index < len(source_positions) or flags[index] & 0x0F:
                raise ValueError("Native index references an invalid source site")
            key = (target_codes[index] << 32) | target_positions[index]
            if key <= previous_key:
                raise ValueError("Duplicate or unsorted native destination")
            native_order.append(index)
            previous_index, previous_key = index, key
        reader.finish()

    return SiteMap(
        tuple(contig.name for contig in metadata.source),
        tuple(contig.name for contig in metadata.target),
        MappingProxyType(spans),
        source_codes,
        source_positions,
        target_codes,
        target_positions,
        flags,
        native_order,
    )


def load_site_map(
    manifest_path: Path,
    *,
    manifest_data: bytes | None = None,
    cache_dir: Path | None = None,
) -> SiteMap:
    """Verify the asset; optionally reuse decoded columns in the library."""
    try:
        if manifest_data is None:
            with manifest_path.open("rb") as source:
                manifest_data = source.read(1024 * 1024 + 1)
        if len(manifest_data) > 1024 * 1024:
            raise ValueError("Manifest exceeds the size limit")
        metadata = _metadata(json.loads(manifest_data))
        path = manifest_path.parent / metadata.filename
        if path.stat().st_size != metadata.compressed_bytes:
            raise ValueError("Compressed length mismatch")
        data = path.read_bytes()
        if len(data) != metadata.compressed_bytes:
            raise ValueError("Compressed length changed while reading")
        if hashlib.sha256(data).hexdigest() != metadata.compressed_sha256:
            raise ValueError("Compressed SHA256 mismatch")
        if cache_dir is not None:
            cached = microarray_site_map_cache.cache_path(cache_dir, manifest_data)
            mapping = microarray_site_map_cache.read(cached, metadata)
            if mapping is not None:
                return mapping
            with ExitStack() as stack:
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    stack.enter_context(build_lock(str(cached) + ".lock"))
                except OSError as error:
                    logging.warning("CHM13 decoded cache unavailable: %s", error)
                    return _decode(data, metadata)
                mapping = microarray_site_map_cache.read(cached, metadata)
                if mapping is None:
                    mapping = _decode(data, metadata)
                    try:
                        microarray_site_map_cache.write(cached, mapping)
                    except OSError as error:
                        logging.warning("Could not save CHM13 decoded cache: %s", error)
                return mapping
        return _decode(data, metadata)
    except (OSError, EOFError, ValueError, TypeError, zlib.error) as error:
        raise WGSExtractError(f"Invalid CHM13 microarray site map: {error}") from error
