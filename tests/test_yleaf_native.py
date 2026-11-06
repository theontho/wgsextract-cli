"""Focused tests of Yleaf-derived matching/QC and the verified lazy cache."""

import hashlib
import io
import json
import tarfile
from collections import Counter
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools
from tests.yleaf_helpers import native_tables as native_tables
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.ydna.yleaf_native import analysis, data, trees
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


def _predictor(tree: str = "yfull") -> Predictor:
    selected = trees.ensure_tree(tree, "hg38")
    return Predictor(selected.tree_file, selected.qc)


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
    predictor = _predictor()
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
    predictor = _predictor(tree)
    assert (
        predictor.predict(link_markers([("1", "leaf", "R1b1", "D")])).haplogroup
        == expected
    )


def _y_vcf(path: Path, positions: tuple[int, ...], contig: str = "Y") -> Path:
    """A single-sample VCF calling G (hemizygous alt) at each A>G marker."""
    path.write_text(
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={contig},length=1000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(f"{contig}\t{p}\t.\tA\tG\t50\tPASS\t.\tGT\t1\n" for p in positions)
    )
    return path


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


def test_tree_pack_downloads_once_verifies_and_repairs_corruption(
    tmp_path, monkeypatch
):
    files = {"tree.json": b"{}", "hg38/new_positions.txt": b"chrY\tM\tR\t1\n"}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stream:
        for name, payload in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            stream.addfile(entry, io.BytesIO(payload))
    blob = buffer.getvalue()
    monkeypatch.setitem(
        trees.MANIFEST["trees"],
        "yfull",
        {
            **trees.MANIFEST["trees"]["yfull"],
            "version": "1.0",
            "url": "https://example.invalid/yfull-1.0.tar.gz",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "files": {
                name: {
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in files.items()
            },
        },
    )
    downloads = []

    def download(url, dest, **kwargs):
        assert kwargs["expected_sha256"] == hashlib.sha256(blob).hexdigest()
        downloads.append(url)
        Path(dest).write_bytes(blob)
        return True

    monkeypatch.setattr(trees, "download_file", download)
    root = trees.ensure_pack("yfull", tmp_path)
    assert root == tmp_path / "yleaf/trees/yfull/1.0"
    assert (root / "tree.json").read_bytes() == b"{}"
    assert trees.ensure_pack("yfull", tmp_path) == root and len(downloads) == 1
    (root / "tree.json").write_text("bad!")
    trees.ensure_pack("yfull", tmp_path)
    assert len(downloads) == 2 and (root / "tree.json").read_bytes() == b"{}"
    leftovers = [p.name for p in root.parent.iterdir()]
    assert not [n for n in leftovers if n.startswith("unpack-") or ".tar" in n]
    monkeypatch.setattr(trees, "download_file", lambda *a, **k: False)
    (root / "tree.json").unlink()
    with pytest.raises(WGSExtractError, match="offline"):
        trees.ensure_pack("yfull", tmp_path)


@pytest.mark.parametrize("tree", ["yfull", "isogg"])
def test_unsupported_build_is_reported_before_any_download(tmp_path, monkeypatch, tree):
    # PR #451 review: the yfull pack skipped ensure_data's build check and failed
    # later with FileNotFoundError on the missing marker table.
    def download(*args, **kwargs):
        raise AssertionError("nothing should be downloaded")

    monkeypatch.setattr(trees, "download_file", download)
    monkeypatch.setattr(data, "download_file", download)
    with pytest.raises(WGSExtractError, match="Unsupported Yleaf build: hg37"):
        trees.ensure_tree(tree, "hg37", tmp_path)


def test_archive_with_an_unexpected_path_is_rejected(tmp_path, monkeypatch):
    blob = _archive("../escape", b"bad")
    asset = {
        "url": "https://example.invalid/hg38.tar.gz",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "files": {"hg38/table.txt": {"size": 3, "sha256": "0" * 64}},
    }
    monkeypatch.setattr(
        data,
        "manifest",
        lambda: {
            "upstream": {"tag": "v4.1.6"},
            "archives": {"hg38": asset, "hg_prediction_tables": asset},
        },
    )
    monkeypatch.setattr(
        data, "download_file", lambda _url, dest, **_: Path(dest).write_bytes(blob)
    )
    with pytest.raises(WGSExtractError, match="Unsafe"):
        data.ensure_data("hg38", tmp_path / "lib")
    assert not list(tmp_path.rglob("escape"))


def test_smoke_cache_gate_needs_only_selected_build_and_shared_tables(
    tmp_path, monkeypatch
):
    from tests.smoke_utils import yleaf_tables_cached
    from wgsextract_cli.core import reference_assets

    root = tmp_path / "yleaf/v4.1.6"
    monkeypatch.setattr(
        reference_assets, "resolve_reference_library", lambda: str(tmp_path)
    )
    monkeypatch.setattr(
        data,
        "manifest",
        lambda: {
            "upstream": {"tag": "v4.1.6"},
            "archives": {
                key: {"files": {f"{key}/table.txt": {}}}
                for key in ("hg19", "hg38", "t2t", "hg_prediction_tables")
            },
        },
    )
    for folder in ("hg38", "hg_prediction_tables"):
        (root / folder).mkdir(parents=True)
        (root / folder / "table.txt").touch()
    assert yleaf_tables_cached("hg38", "isogg")
    assert not yleaf_tables_cached("hg19", "isogg")
    # The yfull tree pack replaces the build tables but must itself be cached.
    assert not yleaf_tables_cached("hg19")
    yfull = trees.MANIFEST["trees"]["yfull"]
    pack = tmp_path / "yleaf/trees/yfull" / yfull["version"]
    for name in yfull["files"]:
        (pack / name).parent.mkdir(parents=True, exist_ok=True)
        (pack / name).touch()
    assert yleaf_tables_cached("hg19")
    (root / "hg_prediction_tables/table.txt").unlink()
    assert not yleaf_tables_cached("hg38", "isogg")


@needs_tools("bcftools")
@pytest.mark.parametrize(
    ("ancient", "custom"), [(False, False), (True, False), (False, True)]
)
def test_run_provenance_identifies_the_actual_marker_panel(
    native_tables, tmp_path, ancient, custom
):
    vcf = _y_vcf(tmp_path / "synthetic.vcf", (100, 110, 120, 130))
    selected = trees.ensure_tree("yfull", "hg38").positions("hg38", ancient)
    if custom:
        panel = tmp_path / "custom-panel.txt"
        panel.write_text(selected.read_text().replace("M-leaf", "M-custom"))
        selected = panel
    output = tmp_path / "results"
    analysis.analyze(
        vcf,
        output,
        build="hg38",
        pos_file=selected if custom else None,
        ancient=ancient,
        no_ref_inference=True,
    )
    metadata = json.loads((output / "run.json").read_text())
    assert metadata["ancient"] is ancient
    assert metadata["marker_panel"] == {
        "source": "custom" if custom else "tree-pack",
        "identifier": selected.name
        if custom
        else selected.relative_to(native_tables).as_posix(),
        "sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
    }
    assert metadata["tree"]["version"] == trees.MANIFEST["trees"]["yfull"]["version"]


@needs_tools("bcftools")
def test_analysis_clears_previous_result_on_failed_run(native_tables, tmp_path):
    vcf = _y_vcf(tmp_path / "no-y.vcf", (), contig="X")
    output = tmp_path / "results"
    output.mkdir()
    (output / "hg_prediction.hg").write_text("old")
    with pytest.raises(WGSExtractError, match="no records on chromosome Y"):
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
    tree_path = trees.ensure_tree(tree, "hg38").tree_file
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
    result = _predictor(tree).predict(link_markers(rows))
    assert tuple(result) == expected


def test_saved_marker_table_prediction_parity(request, tmp_path):
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
        predictor = Predictor(
            Path(library) / "hg_prediction_tables" / "tree.json",
            trees.qc_rules("yfull", Path(library)),
        )
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
    root = Path(library)
    upstream_tree = trees.YleafTree(
        id="yfull",
        name="Yleaf v4.1.6",
        version="v4.1.6",
        date="",
        license="",
        source=library,
        tree_file=root / "hg_prediction_tables" / "tree.json",
        positions_files={build: root / build / "new_positions.txt"},
        qc=trees.qc_rules("yfull", root),
        origin="yleaf-data",
        root=root,
    )
    result = analysis.analyze(
        Path(input_path),
        tmp_path / "native",
        build=build,
        tree=upstream_tree,
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
