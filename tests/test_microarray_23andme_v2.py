import gzip
from pathlib import Path

from wgsextract_cli.commands.microarray import (
    _PACKAGED_ASSETS,
    _resolve_microarray_format,
    _template_search_roots,
)
from wgsextract_cli.core.microarray_utils import (
    convert_to_vendor_format,
    get_template_format,
)

TEMPLATES = _PACKAGED_ASSETS / "microarray" / "raw_file_templates"


def test_v1_and_v2_resolve_to_the_shipped_template():
    assert _resolve_microarray_format("23andme_v1") == "23andMe_V2"
    assert _resolve_microarray_format("23andMe-V2") == "23andMe_V2"
    assert get_template_format("23andMe_V2") == {"suffix": ".txt", "parts": 1}


def test_shipped_template_is_the_build37_marker_list():
    head = (TEMPLATES / "head" / "23andMe_V2.txt").read_text()
    assert "build 37" in head and head.endswith(
        "# rsid\tchromosome\tposition\tgenotype\n"
    )
    with gzip.open(TEMPLATES / "body" / "23andMe_V2.txt.gz", "rt") as f:
        rows = [line.rstrip("\n").split("\t") for line in f]
    assert len(rows) == 574518
    assert rows[0] == ["rs3094315", "1", "752566"]
    assert all(len(row) == 3 for row in rows)
    assert {row[1] for row in rows} == {*map(str, range(1, 23)), "X", "Y", "MT"}


def test_packaged_templates_are_searched_last(tmp_path):
    class Lib:
        root = None

    roots = _template_search_roots(str(tmp_path / "ref.fa"), Lib())  # type: ignore[arg-type]
    assert roots[-1] == str(_PACKAGED_ASSETS)


def test_conversion_reads_the_gzipped_body(tmp_path):
    kit = tmp_path / "kit_hg19.txt"
    kit.write_text("rs3094315\t1\t752566\tAG\nrs3934834\t1\t1005806\tCC\n")
    out = tmp_path / "out.txt"
    roots = [str(tmp_path), str(_PACKAGED_ASSETS)]
    convert_to_vendor_format("23andMe_V2", str(kit), str(out), roots)

    text = out.read_text()
    head = (TEMPLATES / "head" / "23andMe_V2.txt").read_text()
    assert text.startswith(head)
    body = text[len(head) :].splitlines()
    assert len(body) == 574518
    assert body[0] == "rs3094315\t1\t752566\tAG"
    assert "rs3934834\t1\t1005806\tCC" in body[:5]
    assert body[1] == "rs12562034\t1\t768448\t--"


def test_a_library_template_wins_over_the_shipped_one(tmp_path):
    root = Path(tmp_path, "raw_file_templates")
    (root / "head").mkdir(parents=True)
    (root / "body").mkdir()
    (root / "head" / "23andMe_V2.txt").write_text("# library head\n")
    (root / "body" / "23andMe_V2.txt").write_text("rs1\t1\t100\n")
    kit = tmp_path / "kit.txt"
    kit.write_text("rs1\t1\t100\tAA\n")
    out = tmp_path / "out.txt"
    convert_to_vendor_format(
        "23andMe_V2", str(kit), str(out), [str(tmp_path), str(_PACKAGED_ASSETS)]
    )
    assert out.read_text() == "# library head\nrs1\t1\t100\tAA\n"
