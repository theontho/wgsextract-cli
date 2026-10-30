import argparse
import gzip
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyliftover import LiftOver

from scripts.microarray_aadr_markers import build_markers
from tests.htslib_helpers import needs_tools
from wgsextract_cli.commands import microarray
from wgsextract_cli.core import microarray_aadr as aadr
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def markers(tmp_path, monkeypatch):
    source = tmp_path / "markers.tsv.gz"
    with gzip.open(source, "wt") as stream:
        stream.write(
            "# AADR test markers\n"
            "rs1\t1\t0.01\t100\tA\tG\t1\n"
            "rs2\t1\t0.02\t200\tC\tT\t0\n"
            "rs3\t1\t0.03\t300\tG\tA\t1\n"
            "rs4\t1\t0.04\t400\tA\tC\t0\n"
            "rs5\t23\t0.05\t500\tA\tG\t1\n"
        )
    monkeypatch.setattr(aadr, "_MARKERS", source)
    return source


def test_outputs_preserve_aadr_alleles_order_and_reference_dosage(tmp_path, markers):
    calls = tmp_path / "kit.txt"
    calls.write_text(
        "# RSID CHROM POS RESULT\n"
        "different_id\tchr1\t100\tAA\n"
        "rs2\t1\t200\tTC\n"
        "rs3\t1\t300\tAA\n"
        "rs4\t1\t400\tAG\n"
        "rs5\tX\t500\tG\n"
    )
    prefix = str(tmp_path / "kit")
    counts = aadr.write_aadr_outputs("1240k", str(calls), prefix, sex="M")
    assert counts == {"markers": 5, "called": 4, "incompatible": 1}
    assert Path(prefix + ".geno").read_text() == "2\n1\n0\n9\n0\n"
    assert Path(prefix + ".bed").read_bytes() == b"\x6c\x1b\x01\x00\x02\x03\x01\x03"
    assert Path(prefix + ".snp").read_text().splitlines()[:2] == [
        "rs1\t1\t0.01\t100\tA\tG",
        "rs2\t1\t0.02\t200\tC\tT",
    ]
    assert (
        Path(prefix + ".bim").read_text().splitlines()[0] == "1\trs1\t1.00\t100\tA\tG"
    )
    assert Path(prefix + ".fam").read_text() == "0 sample 0 0 1 -9\n"
    assert Path(prefix + ".ind").read_text() == "sample\tM\tUnknown\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_ho_filters_without_reordering_and_missing_sites_are_not_imputed(
    tmp_path, markers
):
    calls = tmp_path / "kit.txt"
    calls.write_text("rs1\t1\t100\tAG\n")
    prefix = str(tmp_path / "ho")
    aadr.write_aadr_outputs("ho", str(calls), prefix)
    assert Path(prefix + ".geno").read_text() == "1\n9\n9\n"
    assert [
        line.split()[0] for line in Path(prefix + ".snp").read_text().splitlines()
    ] == ["rs1", "rs3", "rs5"]
    assert list(aadr.aadr_markers("1240k_ho")) == list(aadr.aadr_markers("1240k"))


@pytest.mark.parametrize("genotype", ["--", "A-", "NN", "AT", "", "AAA"])
def test_invalid_or_out_of_panel_alleles_are_missing(genotype):
    assert aadr.reference_dosage(genotype, "A", "G") == "9"


@pytest.mark.parametrize("sample_id", ["", "../sample", "a b", "a\nb", "é", "a" * 40])
def test_unsafe_sample_labels_fail_before_output(tmp_path, sample_id):
    with pytest.raises(WGSExtractError, match="aadr-sample-id"):
        aadr.write_aadr_outputs(
            "ho", "nonexistent", str(tmp_path / "kit"), sample_id=sample_id
        )
    assert not list(tmp_path.iterdir())


def test_conflicting_calls_at_one_coordinate_are_missing(tmp_path, markers):
    calls = tmp_path / "kit.txt"
    calls.write_text("rs1\t1\t100\tAA\nother\tchr1\t100\tGG\n")
    prefix = str(tmp_path / "conflict")
    aadr.write_aadr_outputs("ho", str(calls), prefix)
    assert Path(prefix + ".geno").read_text() == "9\n9\n9\n"


def test_grch38_forward_mapping_and_inversion_keep_grch37_output(tmp_path, markers):
    chain = tmp_path / "hg19ToHg38.chain"
    chain.write_text("chain 1000 chr1 1000 + 0 1000 chr1 1100 - 0 1000 1\n1000\n\n")
    lifter = LiftOver(str(chain))
    calls = tmp_path / "kit.txt"
    # GRCh37 A/G -> GRCh38 T/C on the inverted chain.
    calls.write_text("rs1\t1\t1001\tTT\nrs2\t1\t901\tGA\n")
    prefix = str(tmp_path / "inverted")
    aadr.write_aadr_outputs("1240k", str(calls), prefix, lifter=lifter)
    assert Path(prefix + ".geno").read_text() == "2\n1\n9\n9\n9\n"
    assert (
        Path(prefix + ".snp").read_text().splitlines()[0] == "rs1\t1\t0.01\t100\tA\tG"
    )


def test_ambiguous_or_alt_contig_liftover_is_not_used(markers):
    marker = next(aadr.aadr_markers("1240k"))

    class Ambiguous:
        def convert_coordinate(self, *_):
            return [("chr1", 99, "+"), ("chr2", 99, "+")]

    class Alternate:
        def convert_coordinate(self, *_):
            return [("chr1_alt", 99, "+")]

    assert aadr._marker_coordinate(marker, Ambiguous()) is None
    assert aadr._marker_coordinate(marker, Alternate()) is None


@needs_tools("bgzip", "tabix")
def test_separate_target_keeps_legacy_ids_and_does_not_modify_source(tmp_path, markers):
    original = tmp_path / "legacy.tab.gz"
    with gzip.open(original, "wt") as stream:
        stream.write("#CHROM\tPOS\tID\nchr1\t100\told_id\nchr20\t100\trs20\n")
    before = original.read_bytes()
    target = aadr.build_aadr_target(["ho"], str(tmp_path), None, str(original))
    assert original.read_bytes() == before
    with gzip.open(target, "rt") as stream:
        text = stream.read()
    assert "chr1\t100\told_id\n" in text
    assert "chr1\t300\trs3\n" in text
    assert "chrX\t500\trs5\n" in text
    assert "chr20\t100\trs20\n" in text
    assert "chr1\t200\trs2\n" not in text


def test_marker_builder_rejects_panel_conflicts_and_has_no_genotypes(tmp_path):
    panel = tmp_path / "1240k.snp"
    ho = tmp_path / "ho.snp"
    out = tmp_path / "out.tsv.gz"
    panel.write_text("rs1 1 0.01 100 A G\nrs2 2 0.02 200 C T\n")
    ho.write_text("rs1 1 0.01 100 A G\n")
    assert build_markers(panel, ho, out) == 2
    with gzip.open(out, "rt") as stream:
        assert stream.read().splitlines()[1:] == [
            "rs1\t1\t0.01\t100\tA\tG\t1",
            "rs2\t2\t0.02\t200\tC\tT\t0",
        ]
    first = out.read_bytes()
    build_markers(panel, ho, out)
    assert out.read_bytes() == first
    ho.write_text("rs1 1 0.01 100 G A\n")
    with pytest.raises(ValueError, match="Panels disagree"):
        build_markers(panel, ho, out)
    assert out.read_bytes() == first


def test_aadr_dispatch_uses_input_build_calls_without_reverse_chain(
    tmp_path, markers, monkeypatch
):
    calls = tmp_path / "input_CombinedKit.txt"
    calls.write_text("rs1\t1\t100\tAA\n")
    monkeypatch.setattr(microarray, "aadr_lifter", lambda *_: None)
    monkeypatch.setattr(microarray, "_template_search_roots", lambda *_: [])
    microarray._convert_microarray_outputs(
        args=argparse.Namespace(formats="AADR-HO,aadr_ho"),
        outdir=str(tmp_path),
        base_name="input",
        lib=SimpleNamespace(build="hg38", liftover_chain=None),
        combined_kit_txt=str(calls),
        ref_fasta="unused",
        start_total=time.time(),
    )
    assert (tmp_path / "input_AADR_ho.geno").read_text() == "2\n9\n9\n"
    assert not (tmp_path / "input_CombinedKit_hg19.txt").exists()


@pytest.mark.parametrize("build", [None, "t2t", "unknown"])
def test_aadr_requires_a_known_supported_build(tmp_path, build):
    with pytest.raises(WGSExtractError, match="known GRCh37 or GRCh38"):
        aadr.aadr_lifter(build, str(tmp_path))


def test_a_bad_marker_table_leaves_no_partial_outputs(tmp_path, markers):
    calls = tmp_path / "calls.txt"
    calls.write_text("")
    with gzip.open(markers, "at") as stream:
        stream.write("malformed\n")
    with pytest.raises(ValueError):
        aadr.write_aadr_outputs("ho", str(calls), str(tmp_path / "broken"))
    assert not list(tmp_path.glob("broken*"))
