"""The genome-wide microarray target built from the chip templates."""

import gzip
import subprocess
from pathlib import Path

import pytest

from wgsextract_cli.core import microarray_targets as mt
from wgsextract_cli.core.dependencies import get_tool_path


def _templates(root: Path) -> Path:
    body = root / "microarray" / "raw_file_templates" / "body"
    body.mkdir(parents=True)
    (body / "23andMe_V3.txt").write_text(
        "rs1\t1\t1000\t\nrs2\t2\t2000\t\nrs3\tX\t3000\t\n"
    )
    # MyHeritage: quoted CSV; rs1's position again (one marker, first rsID kept).
    (body / "MyHeritage_V2.csv").write_text('"rs9","1","1000"\n"rs4","MT","73"\n')
    # 23andMe's API list puts the rsID last.
    (body / "23andMe_SNPs_API.txt").write_text("chr7\t5000\trs5\n")
    # FTDNA's Affymetrix export is NCBI36: its positions must not be targets.
    (body / "FTDNA_V1_Affy.csv").write_text("rs6,3,999999\n")
    return root / "microarray"


def test_markers_are_the_union_of_the_grch37_templates(tmp_path):
    markers = mt.collect_template_markers([str(_templates(tmp_path))])
    assert markers == {
        ("chr1", 1000): "rs1",
        ("chr2", 2000): "rs2",
        ("chrX", 3000): "rs3",
        ("chrM", 73): "rs4",
        ("chr7", 5000): "rs5",
    }


@pytest.mark.skipif(
    not (get_tool_path("bgzip") and get_tool_path("tabix")), reason="htslib missing"
)
def test_hg19_target_is_written_sorted_and_indexed(tmp_path):
    templates = _templates(tmp_path)
    out = mt.build_genome_wide_target(str(tmp_path), "GRCh37", [str(templates)])

    assert out == str(tmp_path / "microarray" / "All_SNPs_hg19_ref.tab.gz")
    assert Path(out + ".tbi").is_file()
    with gzip.open(out, "rt") as f:
        lines = f.read().splitlines()
    assert lines == [
        "#CHROM\tPOS\tID",
        "chr1\t1000\trs1",
        "chr2\t2000\trs2",
        "chr7\t5000\trs5",
        "chrM\t73\trs4",
        "chrX\t3000\trs3",
    ]
    region = subprocess.run(
        [get_tool_path("tabix"), out, "chr2"], capture_output=True, text=True
    )
    assert region.stdout == "chr2\t2000\trs2\n"


def test_no_templates_says_to_bootstrap(tmp_path):
    with pytest.raises(mt.WGSExtractError, match="ref bootstrap"):
        mt.build_genome_wide_target(str(tmp_path), "hg38", [str(tmp_path)])
