import pytest

from tests.htslib_helpers import needs_htslib, run_wgsextract, sam_to_bam
from wgsextract_cli.commands._extract_poz import poz_bed
from wgsextract_cli.core.utils import WGSExtractError, run_command


@needs_htslib
@pytest.mark.parametrize(
    "name,length,left,right",
    [
        ("chrY", 57_227_415, 2_781_558, 3_049_958),
        ("Y", 59_373_566, 2_649_599, 2_917_999),
    ],
)
def test_poz_keeps_overlapping_reads_not_other_y_or_mt(
    tmp_path, name, length, left, right
):
    lines = [f"@SQ\tSN:{name}\tLN:{length}", "@SQ\tSN:chrM\tLN:16569"]
    for label, pos, cigar, seq in [
        ("before", left, "1M", "A"),
        ("left", left + 1, "1M", "A"),
        ("right", right, "1M", "A"),
        ("after", right + 1, "1M", "A"),
        ("overlap", left, "2M", "AA"),
    ]:
        lines.append(
            f"{label}\t0\t{name}\t{pos}\t60\t{cigar}\t*\t0\t0\t{seq}\t{'I' * len(seq)}"
        )
    lines.append("mito\t0\tchrM\t3500\t60\t1M\t*\t0\t0\tA\tI")
    bam = sam_to_bam(lines, tmp_path / "sample.bam")
    out = tmp_path / "o"
    run_wgsextract(
        "extract",
        "ydna-bam",
        "--poz",
        "--input",
        bam,
        "--outdir",
        str(out),
        "--threads",
        "2",
    )
    output = out / "sample_Y_poz.bam"
    records = run_command(["samtools", "view", output], capture_output=True).stdout
    assert {row.split("\t")[0] for row in records.splitlines()} == {
        "left",
        "right",
        "overlap",
    }
    assert (out / "sample_Y_poz.bam.bai").exists()
    assert not (out / "sample_Y.bam").exists()
    assert not list(out.glob(".poz_*"))


@pytest.mark.parametrize(
    "contigs",
    [{"chrY": 62_460_029}, {"chrY": 1000}, {"chr1": 248_387_328, "chrY": 57_227_415}],
)
def test_poz_rejects_unknown_t2t_and_inconsistent_coordinates(tmp_path, contigs):
    with pytest.raises(WGSExtractError, match="GRCh37/hg19"):
        with poz_bed(contigs, "chrY", str(tmp_path)):
            pytest.fail("must not select a BED")
