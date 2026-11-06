"""``panel bed`` (#335): gene lists and presets to padded, merged BED targets."""

from pathlib import Path

import pytest

from wgsextract_cli import api
from wgsextract_cli.core import panel
from wgsextract_cli.core.panel_presets import PRESETS


def _bed(tmp_path: Path, **fields) -> tuple[api.Result, list[list[str]]]:
    result = api.panel.bed(api.PanelBedParams(outdir=tmp_path, **fields))
    (output,) = [o for o in result.outputs if o.role == "panel_bed"]
    rows = [line.split("\t") for line in Path(output.path).read_text().splitlines()]
    return result, rows


def _fai(path: Path, build: str, rename=lambda name: name, **lengths: int) -> Path:
    contigs = dict(panel.load_annotations(build).contigs) | lengths
    Path(f"{path}.fai").write_text(
        "".join(f"{rename(c)}\t{n}\t0\t60\t61\n" for c, n in contigs.items())
    )
    path.touch()
    return path


def _region(rows: list[list[str]], gene: str) -> tuple[str, int, int]:
    row = next(r for r in rows if gene in r[3].split(","))
    return row[0], int(row[1]), int(row[2])


def test_pharmacogenes_are_padded_merged_and_clipped(tmp_path):
    result, rows = _bed(tmp_path, presets=("pharmacogenes",))
    # NCBI RefSeq GRCh38.p14: CYP2D6 is chr22:42,126,499-42,130,810.
    assert _region(rows, "CYP2D6") == ("chr22", 42_026_498, 42_230_810)
    # CYP3A4 and CYP3A5 are 100 kb apart: one region.
    assert _region(rows, "CYP3A4") == _region(rows, "CYP3A5")
    # MT-RNR1 plus 100 kb is all of chrM.
    assert _region(rows, "MT-RNR1") == ("chrM", 0, 16_569)
    assert all(len(r) == 4 for r in rows)
    assert result.metrics["targets"] == len(rows)
    assert result.metrics["loci"] == 25
    assert result.metrics["target_bases"] == sum(int(r[2]) - int(r[1]) for r in rows)
    assert 0 < result.metrics["genome_fraction"] < panel.RECOMMENDED_FRACTION


def test_no_padding_gives_the_gene_span_and_t2t_coordinates(tmp_path):
    _, rows = _bed(tmp_path, genes=("cyp2d6",), pad_kb=0, build="t2t")
    assert rows == [["chr22", "42605989", "42610301", "CYP2D6"]]


@pytest.mark.parametrize("build", panel.BUILDS)
@pytest.mark.parametrize("preset", PRESETS)
def test_every_preset_resolves_in_every_build(preset, build):
    assert panel.preset_loci(preset, panel.load_annotations(build))


def test_contig_names_and_lengths_follow_the_ref(tmp_path):
    ref = _fai(
        tmp_path / "hs37d5.fa",
        "hg19",
        lambda c: "MT" if c == "chrM" else c.removeprefix("chr"),
    )
    result, rows = _bed(tmp_path, genes=("CYP2D6", "MT-RNR1"), ref=ref)
    assert result.metrics["build"] == "hg19"
    assert [r[0] for r in rows] == ["22", "MT"]


def test_ucsc_hg19_chrm_is_targeted_whole(tmp_path):
    ref = _fai(tmp_path / "ucsc.fa", "hg19", chrM=16_571)
    result, rows = _bed(tmp_path, genes=("MT-ND1",), pad_kb=0, ref=ref)
    assert rows == [["chrM", "0", "16571", "MT-ND1"]]
    assert [w.code for w in result.warnings] == ["panel_chrm_not_rcrs"]


def test_ref_of_another_build_is_refused(tmp_path):
    ref = _fai(tmp_path / "hg38.fa", "hg38")
    with pytest.raises(api.CommandFailed, match="--ref is hg38, not --build t2t"):
        api.panel.bed(
            api.PanelBedParams(genes=("CYP2D6",), build="t2t", ref=ref, outdir=tmp_path)
        )
    other = _fai(tmp_path / "other.fa", "hg38", chr7=1_000)
    with pytest.raises(api.CommandFailed, match="chr7 has other lengths"):
        api.panel.bed(api.PanelBedParams(genes=("CYP2D6",), ref=other, outdir=tmp_path))


def test_unknown_and_ambiguous_genes_are_all_reported(tmp_path):
    with pytest.raises(api.CommandFailed) as failed:
        api.panel.bed(
            api.PanelBedParams(genes=("HLA,CYP2D66", "P450C2D"), outdir=tmp_path)
        )
    message = str(failed.value)
    assert "HLA: not a gene" in message and "HLA-A" in message
    assert "CYP2D66: not a gene" in message and "CYP2D6?" in message
    assert "P450C2D: an alias of several genes (CYP2D6, CYP2D7)" in message
    assert not list(tmp_path.glob("*.bed"))


def test_unique_alias_resolves_with_a_warning(tmp_path):
    gene_file = tmp_path / "genes.txt"
    gene_file.write_text("# my panel\nCYPIID6  # alias\n")
    result, rows = _bed(tmp_path, gene_file=gene_file, pad_kb=0)
    assert [r[3] for r in rows] == ["CYP2D6"]
    (warning,) = result.warnings
    assert warning.code == "gene_alias" and warning.details["symbol"] == "CYP2D6"


def test_large_panel_warns(tmp_path):
    result, _ = _bed(tmp_path, presets=("acmg-sf", "autoimmune"), pad_kb=2000)
    assert result.metrics["genome_fraction"] > panel.WARN_FRACTION
    assert [w.code for w in result.warnings] == ["panel_too_large"]


def test_cli_writes_the_bed_and_lists_presets(tmp_path, capsys):
    from tests.htslib_helpers import run_wgsextract

    run_wgsextract("panel", "bed", "--list-presets")
    listing = capsys.readouterr().out
    assert all(f"{name}:" in listing for name in PRESETS)
    assert not list(tmp_path.iterdir())
    run_wgsextract("panel", "bed", "--preset", "hla", "-o", str(tmp_path))
    (bed,) = tmp_path.glob("panel_hla_hg38_pad100kb.bed")
    chrom, start, end, name = bed.read_text().split()
    # The classical MHC, GABBR1 to KIFC1, on GRCh38.
    assert (chrom, name) == ("chr6", "MHC")
    assert int(start) == 29_602_238 - 1 - 100_000
    assert int(end) == 33_409_896 + 100_000
