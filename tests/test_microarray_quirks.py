"""Vendor strand quirks: --quirks vendor writes the markers a template's vendor
revision reported on the reverse strand that way; corrected (the default) never
changes a call."""

from argparse import Namespace

import pytest

from wgsextract_cli.core import microarray_quirks as quirks
from wgsextract_cli.core.microarray_utils import convert_to_vendor_format
from wgsextract_cli.core.utils import WGSExtractError

HEADER = (
    "#revision\tid\tchrom\tposition\treported\tforward\tfixed_in\ttemplates"
    "\texports\tconfirmed_by\n"
)
# 23andMe's 2012-2014 V3 revision reported rsQ's C/T on the reverse strand (G/A
# forward); the 2014-2016 revision fixed it. Only the 23andMe_V3 template stands
# for the reverse-strand revision.
FIXED_LATER = (
    "23andme_v3_960613_a600774c\trsQ\t1\t1000\tAG\tCT\t23andme_v3_991791_caa65ff7"
    "\t23andMe_V3\t100\t23andme_v3_991791_caa65ff7\n"
)


@pytest.fixture
def table(tmp_path, monkeypatch):
    def write(*rows: str):
        path = tmp_path / "strand.tsv"
        path.write_text(HEADER + "".join(rows))
        monkeypatch.setattr(quirks, "STRAND_TABLE", path)
        quirks.load_strand_quirks.cache_clear()
        quirks.vendor_strand_flips.cache_clear()
        return path

    yield write
    quirks.load_strand_quirks.cache_clear()
    quirks.vendor_strand_flips.cache_clear()


def _export(tmp_path, template: str, **kwargs) -> list[str]:
    root = tmp_path / "lib" / "raw_file_templates"
    (root / "head").mkdir(parents=True, exist_ok=True)
    (root / "body").mkdir(exist_ok=True)
    (root / "head" / f"{template}.txt").write_text("# h\n")
    bodies = ["rsQ\t1\t1000\nrsP\t1\t2000\n"]
    if template == "23andMe_V4":
        bodies.append("")
    for part, body in enumerate(bodies, 1):
        suffix = f"_{part}" if len(bodies) > 1 else ""
        (root / "body" / f"{template}{suffix}.txt").write_text(body)
    kit = tmp_path / "kit.txt"
    kit.write_text("# RSID\tCHROM\tPOS\tRESULT\nx\t1\t1000\tCT\ny\t1\t2000\tAC\n")
    out = tmp_path / f"{template}.txt"
    convert_to_vendor_format(template, str(kit), str(out), str(root.parent), **kwargs)
    return out.read_text().splitlines()[1:]


def test_each_template_gets_its_revisions_treatment_in_both_modes(tmp_path, table):
    table(FIXED_LATER)

    assert _export(tmp_path, "23andMe_V3", quirks="vendor") == [
        "rsQ\t1\t1000\tAG",
        "rsP\t1\t2000\tAC",
    ]
    # The 23andMe_V4 template stands for revisions that report rsQ forward.
    assert _export(tmp_path, "23andMe_V4", quirks="vendor") == [
        "rsQ\t1\t1000\tCT",
        "rsP\t1\t2000\tAC",
    ]
    for template in ("23andMe_V3", "23andMe_V4"):
        assert _export(tmp_path, template, quirks="corrected") == [
            "rsQ\t1\t1000\tCT",
            "rsP\t1\t2000\tAC",
        ]


def test_default_output_is_unchanged_by_the_table(tmp_path, table):
    table()
    before = _export(tmp_path, "23andMe_V3")
    table(FIXED_LATER)

    assert (
        _export(tmp_path, "23andMe_V3")
        == before
        == [
            "rsQ\t1\t1000\tCT",
            "rsP\t1\t2000\tAC",
        ]
    )
    assert quirks.quirks_mode(Namespace(quirks=None)) == "corrected"


@pytest.mark.parametrize(
    "row",
    [
        # 23andMe_V4's rs397507452 is relocated by corrections.tsv.
        "23andme_v4_598897_705f7288\trs397507452\tMT\t7443\tAG\tCT\t.\t23andMe_V4"
        "\t10\t23andme_v4_610544_681e6673\n",
        # NCBI36 templates already complement calls in inverted regions.
        "23andme_v3_960613_a600774c\trsQ\t1\t1000\tAG\tCT\t.\tFTDNA_V1_Affy\t10"
        "\t23andme_v4_610544_681e6673\n",
        # An A/T pair cannot show its strand.
        "23andme_v3_960613_a600774c\trsQ\t1\t1000\tAT\tAT\t.\t23andMe_V3\t10"
        "\t23andme_v4_610544_681e6673\n",
        # Every entry needs a confirming forward-strand revision.
        "23andme_v3_960613_a600774c\trsQ\t1\t1000\tAG\tCT\t.\t23andMe_V3\t10\t.\n",
        # ...from another revision: a second probe in the same one doesn't count.
        "23andme_v3_960613_a600774c\trsQ\t1\t1000\tAG\tCT\t.\t23andMe_V3\t10"
        "\t23andme_v3_960613_a600774c\n",
        "23andme_v3_960613_a600774c\trsQ\t1\t1000\tAG\tCT\tunknown_rev\t23andMe_V3"
        "\t10\t23andme_v4_610544_681e6673\n",
    ],
)
def test_table_rejects_entries_that_could_misapply(table, row):
    table(row)

    with pytest.raises(WGSExtractError):
        quirks.load_strand_quirks()


def test_mode_comes_from_config_unless_given(monkeypatch):
    monkeypatch.setitem(quirks.settings, "microarray_quirks", "vendor")

    assert quirks.quirks_mode(Namespace(quirks=None)) == "vendor"
    assert quirks.quirks_mode(Namespace(quirks="corrected")) == "corrected"
    monkeypatch.setitem(quirks.settings, "microarray_quirks", "raw")
    with pytest.raises(WGSExtractError):
        quirks.quirks_mode(Namespace(quirks=None))


def test_packaged_table_loads():
    quirks.load_strand_quirks.cache_clear()

    assert quirks.load_strand_quirks()
