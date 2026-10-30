import gzip
from types import SimpleNamespace

import pytest

from tests.test_microarray_vendor_formats import _kit, _templates
from wgsextract_cli.core import microarray_identity as identity
from wgsextract_cli.core.microarray_utils import convert_to_vendor_format
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def rsid_map(monkeypatch):
    records = {
        "rs10": identity.RsidIdentity("rs100", "merged"),
        "rs11": identity.RsidIdentity("rs100", "merged"),
        "rs20": identity.RsidIdentity(None, "withdrawn"),
        "rs30": identity.RsidIdentity(None, "ambiguous"),
        "rs40": identity.RsidIdentity(None, "unresolved"),
    }
    monkeypatch.setattr(identity, "load_rsid_map", lambda: records)
    return records


def test_identity_configuration_and_explicit_overrides(monkeypatch):
    monkeypatch.setattr(
        identity,
        "settings",
        {"microarray_strand_ambiguous": "no-call", "microarray_resolve_rsids": True},
    )
    policy = identity.identity_policy(
        SimpleNamespace(strand_ambiguous="keep", resolve_rsids=False)
    )
    assert policy.rejection("rs10", "A", "T") is None
    configured = identity.identity_policy(SimpleNamespace())
    assert configured.needs_alleles and configured.resolve_rsids
    monkeypatch.setitem(identity.settings, "microarray_strand_ambiguous", ["keep"])
    with pytest.raises(WGSExtractError, match="keep or no-call"):
        identity.identity_policy(SimpleNamespace())


def test_packaged_map_parser_requires_flat_terminal_identities(tmp_path, monkeypatch):
    path = tmp_path / "ids.tsv.gz"
    monkeypatch.setattr(identity, "_RSID_MAP", path)
    identity.load_rsid_map.cache_clear()
    try:
        with gzip.open(path, "wt") as output:
            output.write("# IDs only\nrs10\trs100\tmerged\nrs20\t.\twithdrawn\n")
        assert identity.resolve_rsid("rs10").current == "rs100"
        assert (
            identity.MicroarrayIdentity(resolve_rsids=True).rejection("rs20")
            == "retired_rsid"
        )
        identity.load_rsid_map.cache_clear()
        with gzip.open(path, "wt") as output:
            output.write("rs10\trs11\tmerged\nrs11\trs100\tmerged\n")
        with pytest.raises(WGSExtractError, match="terminal identities"):
            identity.load_rsid_map()
    finally:
        identity.load_rsid_map.cache_clear()


def test_rsid_fallback_resolves_aliases_but_rejects_retired_identities(
    tmp_path, rsid_map, caplog
):
    root = _templates(
        tmp_path / "lib",
        "FTDNA_V1_Affy.csv",
        "RSID,CHROMOSOME,POSITION,RESULT\n",
        "rs10,1,1000,\nrs20,1,2000,\nrs30,1,3000,\nrs40,1,4000,\n",
    )
    kit = _kit(
        tmp_path / "kit.txt",
        [
            ("rs100", "1", "1000", "AC"),
            ("rs20", "1", "2000", "AA"),
            ("rs30", "1", "3000", "CG"),
            ("rs40", "1", "4000", "GT"),
        ],
    )
    before, after = tmp_path / "before.csv", tmp_path / "after.csv"
    convert_to_vendor_format("FTDNA_V1_Affy", kit, str(before), root)
    convert_to_vendor_format(
        "FTDNA_V1_Affy",
        kit,
        str(after),
        root,
        identity=identity.MicroarrayIdentity(resolve_rsids=True),
    )
    assert before.read_text().splitlines()[1:] == [
        "rs10,1,1000,--",
        "rs20,1,2000,AA",
        "rs30,1,3000,CG",
        "rs40,1,4000,GT",
    ]
    assert after.read_text().splitlines()[1:] == [
        "rs10,1,1000,AC",
        "rs20,1,2000,--",
        "rs30,1,3000,--",
        "rs40,1,4000,--",
    ]
    assert "template_withdrawn" in caplog.text
    assert "template_ambiguous" in caplog.text
    assert "template_unresolved" in caplog.text


@pytest.mark.parametrize("other", ["GG", "--"])
def test_alias_conflicts_never_restore_rejected_calls(tmp_path, rsid_map, other):
    root = _templates(
        tmp_path / "lib", "FTDNA_V1_Affy.csv", "header\n", "rs10,1,1000,\n"
    )
    rows = [("rs10", "1", "1000", "AC"), ("rs100", "1", "1001", other)]
    for index, records in enumerate((rows, list(reversed(rows)))):
        kit = _kit(tmp_path / "kit.txt", records)
        out = tmp_path / f"result-{index}.csv"
        convert_to_vendor_format(
            "FTDNA_V1_Affy",
            kit,
            str(out),
            root,
            identity=identity.MicroarrayIdentity(resolve_rsids=True),
        )
        assert out.read_text() == "header\nrs10,1,1000,--\n"


def test_retired_template_is_not_rescued_by_position_matching(tmp_path, rsid_map):
    root = _templates(
        tmp_path / "lib", "FTDNA_V2.csv", "header\n", '"rs20","1","1000",\n'
    )
    kit = _kit(tmp_path / "kit.txt", [("rs100", "1", "1000", "AC")])
    out = tmp_path / "result.csv"
    convert_to_vendor_format(
        "FTDNA_V2",
        kit,
        str(out),
        root,
        identity=identity.MicroarrayIdentity(resolve_rsids=True),
    )
    assert out.read_text() == 'header\n"rs20","1","1000","--"\n'
