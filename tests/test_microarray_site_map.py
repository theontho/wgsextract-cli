import gzip
import hashlib
import json
from pathlib import Path

import pytest

from wgsextract_cli.core.microarray_site_map import load_site_map
from wgsextract_cli.core.utils import WGSExtractError

# Four chr1 sites (two mapped, one collision, one unmapped), one MT site.
# Native ordering reverses the two mapped chr1 source positions.
_PAYLOAD = (
    b"WGSCMAP\x01\x02\x04"
    b"\x0a\x20\x01\x14"
    b"\x0a\x50\x01\x31"
    b"\x0a\x05"
    b"\x0a\x01"
    b"\x01\x01\x40\x02\x02"
    b"\x03\x02\x01\x08"
)


def _fixture(directory: Path, payload: bytes = _PAYLOAD, **overrides: object) -> Path:
    data = gzip.compress(payload, mtime=0)
    (directory / "sites.bin.gz").write_bytes(data)
    manifest = {
        "format": "wgsextract-chm13-map",
        "version": 1,
        "source_build": "GRCh37-rCRS",
        "target_build": "CHM13v2.0",
        "file": "sites.bin.gz",
        "compressed_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "uncompressed_bytes": len(payload),
        "uncompressed_sha256": hashlib.sha256(payload).hexdigest(),
        "source_contigs": [
            {"name": "chr1", "length": 100, "rows": 4},
            {"name": "chrM", "length": 20, "rows": 1},
        ],
        "target_contigs": [
            {"name": "chr1", "length": 100},
            {"name": "chrM", "length": 20},
        ],
    }
    manifest.update(overrides)
    path = directory / "map.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_bidirectional_mapping_preserves_inversion_and_missing_status(
    tmp_path: Path,
) -> None:
    mapping = load_site_map(_fixture(tmp_path))
    site = mapping.source_lookup("chr1", 20)
    assert site is not None
    assert (
        site.target_chrom,
        site.target_pos,
        site.inverted,
        site.ref_status,
    ) == ("chr1", 5, True, "ref_different")
    assert list(mapping.native_range("chr1", 5, 5)) == [site]
    assert [
        (site.source_chrom, site.source_pos) for site in mapping.native_sites()
    ] == [("chr1", 20), ("chr1", 10), ("chrM", 1)]
    assert [site.status for site in mapping.source_sites()] == [
        "mapped",
        "mapped",
        "collision",
        "unmapped",
        "mapped",
    ]
    collision = mapping.source_lookup("chr1", 30)
    assert collision is not None
    assert collision.target_pos is None
    assert mapping.source_lookup("chr1", 1) is None
    assert mapping.source_lookup("chr1", 100) is None
    assert mapping.source_lookup("chr2", 10) is None
    assert list(mapping.native_range("chr1", 30, 30)) == []
    assert list(mapping.native_range("chr2", 5, 5)) == []
    assert list(mapping.native_range("chr1", 0, 0)) == []
    assert list(mapping.native_range("chr1", 2**32, 2**32)) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"INVALID!" + _PAYLOAD[8:],
        _PAYLOAD[:8] + b"\x03" + _PAYLOAD[9:],
        _PAYLOAD[:9] + b"\x05" + _PAYLOAD[10:],
        _PAYLOAD[:10] + b"\x00" + _PAYLOAD[11:],
        _PAYLOAD[:10] + b"\x65" + _PAYLOAD[11:],
        _PAYLOAD[:10] + b"\x8a\x00" + _PAYLOAD[11:],
        _PAYLOAD[:10] + b"\x80\x80\x80\x80\x80\x00" + _PAYLOAD[11:],
        _PAYLOAD[:10] + b"\xff\xff\xff\xff\x1f" + _PAYLOAD[11:],
        _PAYLOAD[:11] + b"\xff" + _PAYLOAD[12:],
        _PAYLOAD[:11] + b"\x60" + _PAYLOAD[12:],
        _PAYLOAD[:12] + b"\x00" + _PAYLOAD[13:],
        _PAYLOAD[:13] + b"\x13" + _PAYLOAD[14:],
        _PAYLOAD[:13] + b"\xb6\x01" + _PAYLOAD[14:],
        _PAYLOAD[:19] + b"\x15" + _PAYLOAD[20:],
        _PAYLOAD[:27] + b"\x02" + _PAYLOAD[28:],
        _PAYLOAD[:28] + b"\x02\x00\x06",
        _PAYLOAD[:28] + b"\x00\x02\x06",
        _PAYLOAD[:28] + b"\x0c\x01\x08",
        _PAYLOAD[:28] + b"\x04\x01\x08",
        _PAYLOAD[:-1],
        _PAYLOAD + b"\x00",
    ],
    ids=[
        "magic",
        "block-count",
        "row-count",
        "duplicate-source",
        "source-bounds",
        "nonminimal-varint",
        "overlong-varint",
        "overflow-varint",
        "reserved-flags",
        "invalid-ref-status",
        "destination-contig",
        "zero-destination",
        "destination-bounds",
        "unmapped-orientation",
        "native-index-count",
        "duplicate-native",
        "unsorted-native",
        "index-bounds",
        "index-to-collision",
        "truncation",
        "trailing-record",
    ],
)
def test_rejects_malformed_records_even_with_valid_hashes(
    tmp_path: Path, payload: bytes
) -> None:
    with pytest.raises(WGSExtractError, match="Invalid CHM13 microarray site map"):
        load_site_map(_fixture(tmp_path, payload))


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": True},
        {"version": 2},
        {"source_build": "GRCh38"},
        {"target_build": "unknown"},
        {"file": "../sites.bin.gz"},
        {"file": r"..\sites.bin.gz"},
        {"sha256": "0" * 64},
        {"uncompressed_sha256": "0" * 64},
        {"compressed_bytes": True},
        {"compressed_bytes": 17 * 1024 * 1024},
        {"uncompressed_bytes": 65 * 1024 * 1024},
        {"uncompressed_bytes": len(_PAYLOAD) - 1},
        {"uncompressed_bytes": len(_PAYLOAD) + 1},
        {"source_contigs": [{"name": "chr1", "length": 100, "rows": 4_000_001}]},
        {
            "target_contigs": [
                {"name": "chr1", "length": 100},
                {"name": "chr1", "length": 20},
            ]
        },
    ],
)
def test_rejects_invalid_manifest_and_size_or_hash_mismatch(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises(WGSExtractError, match="Invalid CHM13 microarray site map"):
        load_site_map(_fixture(tmp_path, **overrides))


def test_read_only_assets_need_no_cache_and_corruption_is_not_cached(
    tmp_path: Path,
) -> None:
    path = _fixture(tmp_path)
    asset = tmp_path / "sites.bin.gz"
    before = sorted(item.name for item in tmp_path.iterdir())
    asset.chmod(0o444)
    path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        assert list(load_site_map(path).native_range("chrM", 2, 2))
        assert sorted(item.name for item in tmp_path.iterdir()) == before
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)
        asset.chmod(0o644)
    data = asset.read_bytes()
    asset.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(WGSExtractError, match="Compressed SHA256 mismatch"):
        load_site_map(path)


def test_invalid_deflate_stream_fails_cleanly_with_matching_compressed_hash(
    tmp_path: Path,
) -> None:
    path = _fixture(tmp_path)
    data = b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07" + b"\x00" * 8
    (tmp_path / "sites.bin.gz").write_bytes(data)
    manifest = json.loads(path.read_text())
    manifest.update(compressed_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WGSExtractError, match="Invalid CHM13 microarray site map"):
        load_site_map(path)
