from pathlib import Path

from wgsextract_cli.core.microarray_chm13_markers import (
    manifest_exclusions,
    vendor_no_calls,
)


def test_unverified_manifest_markers_remain_known_indels(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "Name,SNP,GenomeBuild,Chr,MapInfo,SourceSeq\n"
        "knownID,[I/D],37,1,10,unverified context\n"
        "invalidCoordinate,[D/I],37,1,not-a-coordinate,\n"
        "sourceNonSNV,[A/AT],37,1,11,short[A/AT]flank\n"
        "realSNP,[A/G],37,1,12,short[A/G]flank\n"
    )
    ids, sites = manifest_exclusions({"23andMe_V5": str(manifest)})
    assert ids == {"knownID", "invalidCoordinate", "sourceNonSNV"}
    assert sites == {
        ("chr1", 10): "unsupported_indel_marker",
        ("chr1", 11): "unsupported_indel_marker",
    }


def test_known_ids_mask_exact_raw_vendor_rows(tmp_path: Path, monkeypatch) -> None:
    from wgsextract_cli.core import microarray_chm13_markers as markers

    template = tmp_path / "body.txt"
    template.write_text("")
    monkeypatch.setattr(
        markers, "_resolve_templates_for_format", lambda *_args: ("", [str(template)])
    )
    monkeypatch.setattr(
        markers,
        "template_rows",
        lambda *_args: iter([("knownID", "23", "00010"), ("other", "1", "20")]),
    )
    assert vendor_no_calls("23andMe_V5", [str(tmp_path)], {"knownID"}) == {
        ("knownID", "23", "00010"): "--"
    }
