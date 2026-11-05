import os
from pathlib import Path

import pytest

from tests.test_microarray_vendor_formats import TWO_BLOCKS, _chain, _lift
from wgsextract_cli.core import microarray_coordinates


def test_coordinate_cache_preserves_exact_output_and_never_caches_genotypes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(
        tmp_path / "c.chain",
        *TWO_BLOCKS,
        "chain 1 chr3 10000 + 0 1000 chr3 10000 - 0 1000 4\n1000",
        "chain 1 chrM 16569 + 0 1000 chrM 16571 + 2 1002 5\n1000",
    )
    rows = [
        ("# sample header",),
        ("rsX", "X", "10", "GG"),
        ("rsInv", "3", "1", "AC"),
        ("rsB", "1", "2500", "CT"),
        ("rsA", "1", "500", "AC"),
        ("rsDuplicate", "1", "500", "TT"),
        ("rsAlt", "2", "10", "AA"),
        ("rsGap", "1", "1500", "TT"),
        ("rsMT", "MT", "500", "CC"),
    ]
    expected = _lift(tmp_path, rows, chain)
    # Legacy chr_to_int() order places MT before the sex chromosomes.
    assert [row[1] for row in expected if len(row) == 4] == [
        "1",
        "1",
        "1",
        "3",
        "MT",
        "X",
    ]
    original = (tmp_path / "kit_hg19.txt").read_bytes()
    library = str(tmp_path / "library")
    assert _lift(tmp_path, rows, chain, reflib_dir=library) == expected
    assert (tmp_path / "kit_hg19.txt").read_bytes() == original
    changed = [(*row[:3], "GG") if len(row) == 4 else row for row in rows]
    changed[2] = ("newID", "3", "1", "AG")
    expected_changed = _lift(tmp_path, changed, chain)

    def fail(_chain):
        pytest.fail("warm cache loaded the chain")

    monkeypatch.setattr(microarray_coordinates, "LiftOver", fail)
    monkeypatch.setattr(microarray_coordinates, "build_lock", fail)
    assert _lift(tmp_path, rows, chain, reflib_dir=library) == expected
    assert _lift(tmp_path, changed, chain, reflib_dir=library) == expected_changed
    assert ["newID", "3", "10000", "CT"] in expected_changed


def test_coordinate_cache_invalidates_for_chain_content_and_source_coordinates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "c.chain"
    original = "chain 1 chr1 10000 + 0 1000 chr1 10000 + 100 1100 1\n1000"
    chain = _chain(path, original)
    library = str(tmp_path / "library")
    rows = [("rsA", "1", "500", "AC")]
    assert _lift(tmp_path, rows, chain, reflib_dir=library) == [
        ["rsA", "1", "600", "AC"]
    ]
    stat = path.stat()
    _chain(path, original.replace("100 1100", "200 1200"))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    assert _lift(tmp_path, rows, chain, reflib_dir=library) == [
        ["rsA", "1", "700", "AC"]
    ]
    assert _lift(tmp_path, [("rsA", "1", "501", "AC")], chain, reflib_dir=library) == [
        ["rsA", "1", "701", "AC"]
    ]
    assert len(list(Path(library).glob("microarray/hg19-coordinates/*.bin"))) == 3


def test_truncated_coordinate_cache_rebuilds_without_partial_publication(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "c.chain", *TWO_BLOCKS)
    library = str(tmp_path / "library")
    rows = [("rsA", "1", "500", "AC")]
    expected = _lift(tmp_path, rows, chain, reflib_dir=library)
    path = next(Path(library).glob("microarray/hg19-coordinates/*.bin"))
    valid = path.read_bytes()
    path.write_bytes(valid[:-1])
    assert _lift(tmp_path, rows, chain, reflib_dir=library) == expected
    assert path.read_bytes() == valid
    assert not list(path.parent.glob("coordinates-*/"))


@pytest.mark.parametrize("unavailable", ["lock", "publication"])
def test_coordinate_cache_is_optional_when_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    chain = _chain(tmp_path / "c.chain", *TWO_BLOCKS)
    rows = [("rsA", "1", "500", "AC")]
    expected = _lift(tmp_path, rows, chain)

    def denied(*_args):
        raise PermissionError("read-only cache")

    if unavailable == "lock":
        monkeypatch.setattr(microarray_coordinates, "build_lock", denied)
    else:
        monkeypatch.setattr(microarray_coordinates.os, "replace", denied)
    assert (
        _lift(tmp_path, rows, chain, reflib_dir=str(tmp_path / "library")) == expected
    )


def test_coordinate_builder_errors_are_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = _chain(tmp_path / "c.chain", *TWO_BLOCKS)

    def unreadable(_chain):
        raise OSError("cannot read chain")

    # The cache must not mistake pyliftover's error for an unwritable cache.
    monkeypatch.setattr(microarray_coordinates, "LiftOver", unreadable)
    with pytest.raises(OSError, match="cannot read chain"):
        microarray_coordinates.project_coordinates(
            [("chr1", 500)], chain, str(tmp_path / "library")
        )
