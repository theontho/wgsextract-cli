"""Focused tests of Yleaf-derived matching/QC and the verified lazy cache."""

import hashlib
import io
import tarfile
from collections import Counter
from pathlib import Path

import pytest

from tests.yleaf_helpers import native_tables as native_tables
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.ydna.yleaf_native import analysis, data
from wgsextract_cli.ydna.yleaf_native.markers import (
    Call,
    Marker,
    ad_call,
    get_frequencies,
    gt_call,
    match_markers,
    pileup_call,
    read_markers,
    write_marker_tables,
)
from wgsextract_cli.ydna.yleaf_native.predict import (
    Predictor,
    link_markers,
    prediction_row,
    read_yleaf_out_file,
)


def test_vcf_calling_preserves_upstream_ad_denominator_and_gt_first_allele():
    assert ad_call("A", "G,T", "10,9,9") == Call("A", 10, 52.6)
    assert ad_call("A", "G,T", "0,10,10") == Call("G", 10, 100.0)
    assert ad_call("A", "G", ".,.") == Call("A", 0, 0)
    assert gt_call("A", "G,T", "2|1") == Call("T", 1, 100.0)
    assert gt_call("A", "G", "./1") is None


def test_pileup_ignores_indel_payloads_and_mapping_quality_characters():
    counts = get_frequencies("^FAa+12ACGTACGTACGTt-2gcG*$")
    assert counts == {"A": 2, "T": 1, "G": 1, "C": 0, "-": 2, "+": 1}
    assert pileup_call("G", 4, ".,aA") == Call("A", 4, 50)
    # Read threshold uses total pileup depth, not the majority base depth.
    assert pileup_call("N", 10, "AAAAAAAAAT") == Call("A", 10, 90)


def test_matching_filters_sequentially_and_never_infers_failed_observed_sites():
    markers = [Marker("Y", str(p), "R", p, "A>G", "A", "G") for p in range(1, 7)]
    calls = {
        1: Call("G", 0, 100),
        2: Call("T", 20, 100),
        3: Call("G", 9, 100),
        4: Call("G", 10, 89.9),
        5: Call("G", 10, 90),
    }
    passing, failed = match_markers(markers, calls, reference_sequence="AAAAAG")
    assert [(o.marker.pos, o.state, o.call.reads) for o in passing] == [
        (5, "D", 10),
        (6, "D", 0),
    ]
    assert [o.description for o in failed] == [
        "Position with zero reads",
        "Discordant genotype",
        "Below read threshold",
        "Below base majority",
    ]


def test_scoring_excludes_recurrence_and_approximate_nodes_and_resets_cache(
    native_tables,
):
    predictor = Predictor(native_tables / "hg_prediction_tables")
    markers = read_markers(native_tables / "hg38/new_positions.txt")
    assert len(markers) == 9
    calls = {
        pos: Call("G" if pos < 140 or pos >= 151 else "A", 20, 100)
        for pos in (
            100,
            110,
            120,
            130,
            140,
            150,
            151,
            160,
        )
    }
    passing, failed = match_markers(markers, calls)
    ordered = write_marker_tables(
        native_tables, "synthetic", predictor.tree, passing, failed
    )
    linked = link_markers(
        (str(o.marker.pos), o.marker.name, o.marker.haplogroup, o.state)
        for o in ordered
    )
    assert sum(h.nr_total for h in linked.values()) == 6
    scores = predictor.predict(linked)
    assert tuple(scores) == ("R1b1", [], 1.0, 1.0, 1.0, 1.0, 4)
    assert prediction_row("synthetic", linked, scores, "VCF") == [
        "synthetic",
        "R1b1",
        "M-leaf",
        "VCF",
        "6",
        "1.0",
        "1.0",
        "1.0",
        "1.0",
    ]
    # The same predictor instance must not reuse QC1 from the previous sample.
    bad = link_markers([("1", "R", "R", "A"), ("2", "leaf", "R1b1", "D")])
    assert predictor.predict(bad).haplogroup == "NA"


@pytest.mark.parametrize("tree,expected", [("yfull", "R1b1"), ("ftdna", "NA")])
def test_missing_intermediate_ancestors_are_neutral_except_for_ftdna(
    native_tables, tree, expected
):
    predictor = Predictor(native_tables / "hg_prediction_tables", tree)
    assert (
        predictor.predict(link_markers([("1", "leaf", "R1b1", "D")])).haplogroup
        == expected
    )


def _archive(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        entry = tarfile.TarInfo(name)
        entry.size = len(payload)
        stream.addfile(entry, io.BytesIO(payload))
    return buffer.getvalue()


def test_lazy_cache_downloads_selected_build_verifies_and_repairs_corruption(
    tmp_path, monkeypatch
):
    assets = {}
    blobs = {}
    for folder in ("hg19", "hg38", "t2t", "hg_prediction_tables"):
        name, payload = f"{folder}/table.txt", folder.encode()
        blob = _archive(name, payload)
        url = f"https://example.invalid/{folder}.tar.gz"
        blobs[url] = blob
        assets[folder] = {
            "url": url,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "files": {
                name: {
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            },
        }
    monkeypatch.setattr(
        data,
        "manifest",
        lambda: {
            "upstream": {"tag": "v4.1.6"},
            "archives": assets,
        },
    )
    downloaded = []

    def download(url, dest, **kwargs):
        blob = blobs[url]
        assert kwargs["expected_sha256"] == hashlib.sha256(blob).hexdigest()
        downloaded.append(url)
        Path(dest).write_bytes(blob)
        return True

    monkeypatch.setattr(data, "download_file", download)
    root = data.ensure_data("hg38", tmp_path)
    assert len(downloaded) == 2 and not (root / "hg19").exists()
    assert data.ensure_data("hg38", tmp_path) == root and len(downloaded) == 2
    (root / "hg38/table.txt").write_text("bad!")
    data.ensure_data("hg38", tmp_path)
    assert len(downloaded) == 3 and (root / "hg38/table.txt").read_text() == "hg38"
    monkeypatch.setattr(data, "download_file", lambda *a, **k: False)
    with pytest.raises(WGSExtractError, match="offline"):
        data.ensure_data("t2t", tmp_path)


def test_archive_rejects_unexpected_paths(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(_archive("../escape", b"bad"))
    with pytest.raises(WGSExtractError, match="Unsafe"):
        data._extract(archive, tmp_path / "staging", {})


def test_analysis_clears_previous_result_on_failed_run(
    native_tables, tmp_path, monkeypatch
):
    vcf = tmp_path / "synthetic.vcf"
    vcf.touch()
    output = tmp_path / "results"
    output.mkdir()
    (output / "hg_prediction.hg").write_text("old")

    def fail(_):
        raise WGSExtractError("No Y")

    monkeypatch.setattr(analysis, "_query_vcf", fail)
    with pytest.raises(WGSExtractError, match="No Y"):
        analysis.analyze(vcf, output, build="hg38", no_ref_inference=True)
    assert not (output / "hg_prediction.hg").exists()


@pytest.mark.parametrize("tree", ("yfull", "yfull_v10", "ftdna", "isogg"))
def test_upstream_v416_prediction_parity(native_tables, request, monkeypatch, tree):
    """Optional live parity; no upstream package or 447 MB download required in CI."""
    checkout = request.config.getoption("--yleaf-upstream")
    if not checkout:
        pytest.skip(
            "Pass --yleaf-upstream <v4.1.6 checkout> to compare tagged upstream."
        )
    monkeypatch.syspath_prepend(str(Path(checkout).resolve()))
    from yleaf import predict_haplogroup as upstream
    from yleaf import yleaf_constants

    monkeypatch.setattr(
        yleaf_constants, "HG_PREDICTION_FOLDER", native_tables / "hg_prediction_tables"
    )
    tree_path = native_tables / "hg_prediction_tables" / analysis.TREE_FILES[tree]
    monkeypatch.setattr(upstream, "QC1_SCORE_CACHE", {})
    upstream.read_backbone_groups(tree_path)
    rows = [
        ("100", "M-R", "R", "D"),
        ("110", "M-R1", "R1", "D"),
        ("120", "M-R1b", "R1b", "D"),
        ("130", "M-leaf", "R1b1", "D"),
        ("140", "M-child", "R1b2", "A"),
        ("150", "M-C", "C", "A"),
    ]
    haplotypes = {}
    for _, marker, hg, state in rows:
        haplotypes.setdefault(hg, upstream.HgMarkersLinker()).add(marker, state)
    expected = upstream.get_most_likely_haplotype(
        upstream.Tree(tree_path),
        haplotypes,
        0.95,
    )
    result = Predictor(native_tables / "hg_prediction_tables", tree).predict(
        link_markers(rows)
    )
    assert tuple(result) == expected


def test_saved_marker_table_prediction_parity(request, tmp_path, monkeypatch):
    """Replay QC and optionally extraction against supplied upstream real output."""
    folder = request.config.getoption("--yleaf-parity-output")
    if not folder:
        pytest.skip(
            "Pass --yleaf-parity-output <upstream run> and --yleaf-library <data root>."
        )
    library = request.config.getoption("--yleaf-library")
    if not library:
        pytest.fail("--yleaf-library is required with --yleaf-parity-output")
    import csv

    with (Path(folder) / "hg_prediction.hg").open() as stream:
        expected = list(csv.DictReader(stream, delimiter="\t"))
    for row in expected:
        sample = row["Sample_name"]
        linked = read_yleaf_out_file(Path(folder) / sample / f"{sample}.out")
        predictor = Predictor(Path(library) / "hg_prediction_tables")
        actual = dict(
            zip(
                analysis.PREDICTION_HEADER,
                prediction_row(
                    sample, linked, predictor.predict(linked), row["Total_reads"]
                ),
                strict=True,
            )
        )
        for key in ("Hg", "Valid_markers", "QC-score", "QC-1", "QC-2", "QC-3"):
            assert actual[key] == row[key], (sample, key, actual[key], row[key])
    input_path = request.config.getoption("--yleaf-parity-input")
    if not input_path:
        return
    build = request.config.getoption("--yleaf-parity-build")
    if not build:
        pytest.fail("--yleaf-parity-build is required with --yleaf-parity-input")
    reference = request.config.getoption("--yleaf-parity-ref")
    monkeypatch.setattr(analysis, "ensure_data", lambda *a, **k: Path(library))
    result = analysis.analyze(
        Path(input_path),
        tmp_path / "native",
        build=build,
        reference=Path(reference) if reference else None,
        no_ref_inference=not reference,
    )
    with result.open() as stream:
        extracted = list(csv.DictReader(stream, delimiter="\t"))
    assert len(extracted) == len(expected)
    for row, actual in zip(expected, extracted, strict=True):
        for key in row:
            if key != "Hg_marker":  # Upstream truncates an unordered marker set.
                assert actual[key] == row[key], (key, actual[key], row[key])
        sample = row["Sample_name"]
        for suffix in ("out", "fmf"):

            def rows(path):
                with path.open() as stream:
                    return Counter(
                        tuple(
                            (
                                key,
                                float(value)
                                if key in ("reads", "called_perc") and value != "NA"
                                else value,
                            )
                            for key, value in item.items()
                        )
                        for item in csv.DictReader(stream, delimiter="\t")
                    )

            assert rows(Path(folder) / sample / f"{sample}.{suffix}") == rows(
                result.parent / sample / f"{sample}.{suffix}"
            ), suffix
