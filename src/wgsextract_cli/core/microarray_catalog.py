"""Compact, chain-exact coordinate catalogs for the legacy microarray builds.

The catalogs cache coordinates, not vendor rows, marker identities or genotypes.
Every placement is retained in chain order; callers still choose their existing
first-hit or unique-hit policy. Uncatalogued positions and custom chains use the
original chain, loaded lazily.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import sys
from array import array
from bisect import bisect_left
from itertools import accumulate
from pathlib import Path
from typing import Any, cast

from pyliftover import LiftOver as _LiftOver

from wgsextract_cli.core.utils import WGSExtractError

_ASSETS = Path(__file__).resolve().parents[1] / "assets/microarray/catalog"
_MAGIC = b"WGSCAT\x03"
_MAX_ROWS = 4_000_000
_MAX_HITS = 8_000_000
_MAX_COMPRESSED = 32 * 1024 * 1024
_MAX_DECODED = 192 * 1024 * 1024
_POSITION_MASK = (1 << 32) - 1
Hit = tuple[str, int, str, int]


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def integer(self) -> int:
        value = 0
        for shift in range(0, 64, 7):
            if self.offset >= len(self.data):
                raise ValueError("truncated integer")
            byte = self.data[self.offset]
            self.offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                if value >= 1 << 64:
                    raise ValueError("integer overflow")
                return value
        raise ValueError("integer overflow")

    def text(self) -> str:
        length = self.integer()
        if not 1 <= length <= 1024 or self.offset + length > len(self.data):
            raise ValueError("invalid contig name length")
        value = self.data[self.offset : self.offset + length].decode("utf-8")
        self.offset += length
        return value

    def column(self, typecode: str, count: int) -> array:
        values = array(typecode)
        end = self.offset + count * values.itemsize
        if end > len(self.data):
            raise ValueError("truncated catalog column")
        encoded = self.data[self.offset : end]
        if values.itemsize == 1:
            values.frombytes(encoded)
        else:
            decoded = bytearray(len(encoded))
            for lane in range(values.itemsize):
                decoded[lane :: values.itemsize] = encoded[
                    lane * count : (lane + 1) * count
                ]
            values.frombytes(decoded)
        self.offset = end
        if sys.byteorder != "little":
            values.byteswap()
        return values


class _Catalog:
    def __init__(self, data: bytes):
        if not data.startswith(_MAGIC):
            raise ValueError("invalid catalog magic")
        reader = _Reader(data[len(_MAGIC) :])
        rows, contigs = reader.integer(), reader.integer()
        if not 1 <= rows <= _MAX_ROWS or not 1 <= contigs <= 4096:
            raise ValueError("invalid catalog dimensions")
        self.chroms = tuple(reader.text() for _ in range(contigs))
        if len(set(self.chroms)) != contigs:
            raise ValueError("duplicate contig")
        self.chrom_codes = {chrom: code for code, chrom in enumerate(self.chroms)}
        hits = reader.integer()
        if hits > _MAX_HITS:
            raise ValueError("too many placements")
        # Fixed-width delta columns avoid millions of Python varint calls on
        # every cold start. Gzip compresses their high zero bytes efficiently.
        key_deltas = reader.column("Q", rows)
        if any(delta == 0 for delta in key_deltas[1:]):
            raise ValueError("source coordinates are not strictly ordered")
        self.keys = array("Q", accumulate(key_deltas))
        self.starts = array("I", accumulate(reader.column("I", rows), initial=0))
        if self.starts[-1] != hits:
            raise ValueError("invalid placement offsets")
        self.destinations = array("Q", accumulate(reader.column("q", hits)))
        self.scores = reader.column("Q", hits)
        self.reverse = reader.column("B", hits)
        if any(key >> 32 >= contigs for key in self.keys):
            raise ValueError("invalid source coordinate")
        if any(key >> 32 >= contigs for key in self.destinations) or any(
            strand not in (0, 1) for strand in self.reverse
        ):
            raise ValueError("invalid placement")
        if reader.offset != len(reader.data):
            raise ValueError("trailing catalog bytes")

    def lookup(self, chrom: str, position: int, strand: str) -> list[Hit] | None:
        code = self.chrom_codes.get(chrom)
        if code is None or not 0 <= position <= _POSITION_MASK:
            return None
        key = code << 32 | position
        index = bisect_left(self.keys, key)
        if index == len(self.keys) or self.keys[index] != key:
            return None
        return [
            (
                self.chroms[self.destinations[hit] >> 32],
                self.destinations[hit] & _POSITION_MASK,
                "-" if bool(self.reverse[hit]) != (strand == "-") else "+",
                self.scores[hit],
            )
            for hit in range(self.starts[index], self.starts[index + 1])
        ]


@functools.lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    path = _ASSETS / "manifest.json"
    if not path.is_file():
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise WGSExtractError("Microarray coordinate catalog manifest is too large.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["format"] != "wgsextract-legacy-coordinate-catalog-v3":
            raise ValueError("unsupported format")
        if not isinstance(manifest["maps"], dict):
            raise ValueError("invalid map index")
        return dict(manifest["maps"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise WGSExtractError(
            f"Invalid microarray coordinate catalog: {error}"
        ) from error


@functools.lru_cache(maxsize=3)
def _load_catalog(chain_digest: str) -> _Catalog | None:
    spec = _manifest().get(chain_digest)
    if spec is None:
        return None
    try:
        name = spec["filename"]
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("invalid catalog filename")
        path = _ASSETS / name
        if not 1 <= path.stat().st_size <= _MAX_COMPRESSED:
            raise ValueError("compressed catalog is too large")
        compressed = path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != spec["sha256"]:
            raise ValueError("compressed catalog checksum mismatch")
        with gzip.open(path, "rb") as source:
            decoded = source.read(_MAX_DECODED + 1)
        if len(decoded) > _MAX_DECODED:
            raise ValueError("decoded catalog is too large")
        if hashlib.sha256(decoded).hexdigest() != spec["decoded_sha256"]:
            raise ValueError("decoded catalog checksum mismatch")
        catalog = _Catalog(decoded)
        if len(catalog.keys) != spec["rows"]:
            raise ValueError("catalog row count mismatch")
        return catalog
    except (OSError, EOFError, ValueError, KeyError, TypeError, OverflowError) as error:
        raise WGSExtractError(
            f"Invalid microarray coordinate catalog: {error}"
        ) from error


@functools.lru_cache(maxsize=16)
def _chain_digest(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class CatalogLiftOver:
    """The subset of the pyliftover interface used by microarray projections."""

    def __init__(self, chain: str):
        self.chain = chain
        stat = Path(chain).stat()
        digest = _chain_digest(chain, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        self.catalog = _load_catalog(digest)
        self._lifter = _LiftOver(chain) if self.catalog is None else None

    def convert_coordinate(
        self, chromosome: str, position: int, strand: str = "+"
    ) -> list[Hit] | None:
        if strand not in ("+", "-"):
            # Keep pyliftover's argument validation rather than interpreting an
            # invalid strand as the forward strand.
            return cast(
                list[Hit] | None,
                self._fallback().convert_coordinate(chromosome, position, strand),
            )
        if self.catalog is not None:
            hits = self.catalog.lookup(chromosome, position, strand)
            if hits is not None:
                return hits
        return cast(
            list[Hit] | None,
            self._fallback().convert_coordinate(chromosome, position, strand),
        )

    def _fallback(self) -> _LiftOver:
        if self._lifter is None:
            self._lifter = _LiftOver(self.chain)
        return self._lifter
