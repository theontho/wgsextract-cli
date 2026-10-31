import gzip
import hashlib
import json

import pytest
from pyliftover import LiftOver

from scripts.build_microarray_catalog import build_map
from wgsextract_cli.core import microarray_aadr as aadr
from wgsextract_cli.core import microarray_catalog as catalog
from wgsextract_cli.core.microarray_utils import ncbi36_to_grch37
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def compiled(tmp_path, monkeypatch):
    chain = tmp_path / "chain.gz"
    chain.write_bytes(
        gzip.compress(
            b"chain 1000 chr1 1000 + 0 500 chr1 2000 + 100 600 1\n500\n\n"
            b"chain 900 chr1 1000 + 0 500 chr2 2000 - 0 500 2\n500\n\n"
            b"chain 800 chr1 1000 + 500 1000 chr1 2000 + 100 600 3\n500\n\n"
            b"chain 700 chrM 16569 + 0 16569 chrM 18000 + 100 16669 4\n16569\n\n"
        )
    )
    sites = [
        ("chr1", 0),
        ("chr1", 99),
        ("chr1", 499),
        ("chr1", 500),
        ("chr1", 999),
        ("chr1", 1000),
        ("chrM", 99),
    ]
    asset = tmp_path / "map.bin.gz"
    spec = build_map(sites, chain, asset)
    digest = hashlib.sha256(chain.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "format": "wgsextract-legacy-coordinate-catalog-v3",
                "maps": {digest: spec},
            }
        )
    )
    monkeypatch.setattr(catalog, "_ASSETS", tmp_path)
    catalog._manifest.cache_clear()
    catalog._load_catalog.cache_clear()
    yield chain, asset, sites, spec
    catalog._manifest.cache_clear()
    catalog._load_catalog.cache_clear()


def test_compiled_hits_preserve_chain_order_scores_inversions_and_mito(compiled):
    chain, _, sites, _ = compiled
    original = LiftOver(str(chain))
    cached = catalog.CatalogLiftOver(str(chain))
    assert cached.catalog is not None
    for chrom, pos in reversed(sites):
        for strand in ("+", "-"):
            assert cached.convert_coordinate(chrom, pos, strand) == (
                original.convert_coordinate(chrom, pos, strand) or []
            )
    # Explicit unmapped positions must not trigger the fallback or hom-ref fill.
    assert cached.convert_coordinate("chr1", 1000) == []
    assert cached.convert_coordinate("chrM", 99)[0][:3] == ("chrM", 199, "+")
    assert cached._lifter is None


def test_static_multiple_hits_and_collision_partners_survive_filtered_queries(compiled):
    chain, _, _, spec = compiled
    cached = catalog.CatalogLiftOver(str(chain))
    # Querying only one of the source positions cannot remove its second hit or
    # promote an ambiguous placement to AADR's uniquely placed marker policy.
    marker = aadr.AADRMarker("rs1", "1", "0", 100, "A", "G", True)
    assert len(cached.convert_coordinate("chr1", 99)) == 2
    assert aadr._marker_coordinate(marker, cached) is None
    assert ncbi36_to_grch37(cached, "1", "100") == ("1", "200", False)
    assert (
        cached.convert_coordinate("chr1", 0)[0][:3]
        == cached.convert_coordinate("chr1", 500)[0][:3]
    )
    assert spec["counts"]["collision_destinations"] > 0
    assert cached._lifter is None


def test_custom_positions_use_the_original_chain_only_when_needed(compiled):
    chain, _, _, _ = compiled
    cached = catalog.CatalogLiftOver(str(chain))
    original = LiftOver(str(chain))
    assert cached._lifter is None
    for chrom, pos in (("chr1", 250), ("chrM", 250), ("unknown", 1), ("chr1", -1)):
        assert cached.convert_coordinate(chrom, pos) == original.convert_coordinate(
            chrom, pos
        )
    assert cached._lifter is not None


def test_changed_chain_never_uses_an_incompatible_catalog(compiled):
    chain, _, _, _ = compiled
    # Identical logical mappings but a different pinned chain: the cache must
    # not be selected by its filename or a previous instance's path alone.
    chain.write_bytes(
        gzip.compress(b"chain 999 chr1 1000 + 0 500 chr1 2000 + 200 700 1\n500\n\n")
    )
    cached = catalog.CatalogLiftOver(str(chain))
    assert cached.catalog is None
    assert cached.convert_coordinate("chr1", 99)[0][1] == 299


def test_known_catalog_corruption_fails_closed(compiled):
    chain, asset, _, _ = compiled
    asset.write_bytes(asset.read_bytes()[:-1])
    with pytest.raises(WGSExtractError, match="checksum"):
        catalog.CatalogLiftOver(str(chain))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("filename", "../map.bin.gz", "filename"),
        ("rows", 123, "row count"),
        ("decoded_sha256", "0" * 64, "decoded catalog checksum"),
    ],
)
def test_invalid_manifest_metadata_cannot_select_incorrect_coordinates(
    compiled, field, value, message
):
    chain, _, _, _ = compiled
    manifest_path = chain.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    spec = next(iter(manifest["maps"].values()))
    spec[field] = value
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(WGSExtractError, match=message):
        catalog.CatalogLiftOver(str(chain))


def test_catalog_decompression_is_bounded(compiled, monkeypatch):
    chain, _, _, _ = compiled
    monkeypatch.setattr(catalog, "_MAX_DECODED", 10)
    with pytest.raises(WGSExtractError, match="decoded catalog is too large"):
        catalog.CatalogLiftOver(str(chain))


@pytest.mark.parametrize("damage", ["truncated", "trailing", "magic"])
def test_malformed_decoded_catalog_is_rejected(compiled, damage):
    _, asset, _, _ = compiled
    decoded = gzip.decompress(asset.read_bytes())
    damaged = {
        "truncated": decoded[:-1],
        "trailing": decoded + b"\x00",
        "magic": b"invalid" + decoded[7:],
    }[damage]
    with pytest.raises(ValueError):
        catalog._Catalog(damaged)
