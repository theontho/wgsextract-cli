"""``info calculate-coverage --by`` (#336): per-target depth and enrichment."""

import csv
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam
from wgsextract_cli import api
from wgsextract_cli.core.utils import UsageError

needs_samtools = needs_tools("samtools")


def _sample(tmp_path: Path) -> str:
    """chr1 depths 1..20: 1 1 3 3 3 2 2 3 1 2 2 2 1 1 2 1 0 0 0 1; chrM 50x."""
    lines = ["@SQ\tSN:chr1\tLN:20", "@SQ\tSN:chrM\tLN:10"]
    reads = [
        ("r1", 0, 1, "5M2D5M"),  # the deletion adds no depth
        ("r2", 99, 3, "6M"),
        ("r2", 147, 3, "6M"),  # overlapping mates both count
        ("r3", 1024, 1, "4M"),  # duplicates don't
        ("r4", 0, 10, "3M2I3M"),
        ("r5", 0, 15, "2M3N2M"),
    ]
    for name, flag, pos, cigar in reads:
        length = sum(int(n) for n, op in _ops(cigar) if op in "MI")
        lines.append(
            f"{name}\t{flag}\tchr1\t{pos}\t60\t{cigar}\t*\t0\t0\t{'A' * length}\t{'I' * length}"
        )
    lines += [
        f"m{i}\t0\tchrM\t1\t60\t10M\t*\t0\t0\tAAAAAAAAAA\tIIIIIIIIII" for i in range(50)
    ]
    return sam_to_bam(lines, tmp_path / "s.bam")


def _ops(cigar: str):
    import re

    return re.findall(r"(\d+)([MIDNS])", cigar)


def _coverage(tmp_path: Path, bed: str, **fields) -> tuple[api.Result, list[dict]]:
    path = tmp_path / "panel.bed"
    path.write_text(bed)
    result = api.info.calculate_coverage(
        api.CalculateCoverageParams(
            input=Path(_sample(tmp_path)), by=path, outdir=tmp_path / "o", **fields
        )
    )
    (table,) = [o for o in result.outputs if o.role == "target_coverage"]
    with open(table.path, newline="") as stream:
        return result, list(csv.DictReader(stream, delimiter="\t"))


@needs_samtools
def test_per_target_depth_enrichment_and_low_targets(tmp_path):
    result, rows = _coverage(
        tmp_path,
        "track name=panel\nchr1\t2\t8\tA\nchr1\t5\t12\tB\n1\t15\t20\n",
        min_depth=2,
    )
    by_name = {r["Name"]: r for r in rows}
    # A: depths 3 3 3 2 2 3; B: 2 2 3 1 2 2 2; 1:16-20: 1 0 0 0 1.
    assert by_name["A"]["Mean depth"] == "2.67"
    assert by_name["A"]["Median depth"] == "3.0"
    assert (by_name["B"]["Mean depth"], by_name["B"]["Fraction >=min"]) == (
        "2.00",
        "0.8571",
    )
    unnamed = by_name["1:16-20"]
    assert (unnamed["Contig"], unnamed["Median depth"], unnamed["Low"]) == (
        "chr1",
        "0.0",
        "true",
    )
    metrics = result.metrics
    # On target (merged 3-12, 16-20): 25 depth over 15 bases; off target
    # (1-2, 13-15): 6 over 5. chrM's 50x is left out.
    assert metrics["on_target_mean_depth"] == pytest.approx(25 / 15, abs=1e-3)
    assert metrics["off_target_mean_depth"] == pytest.approx(6 / 5)
    assert metrics["enrichment"] == pytest.approx((25 / 15) / (6 / 5), abs=0.01)
    assert metrics["targets_below_min_depth"] == 1
    assert [w.code for w in result.warnings] == ["targets_below_min_depth"]
    summary = next(o for o in result.outputs if o.role == "target_summary")
    assert "Enrichment (on/off-target mean): 1.4x" in Path(summary.path).read_text()


@needs_samtools
def test_whole_contig_targets_have_no_off_target(tmp_path):
    result, rows = _coverage(tmp_path, "chr1\t0\t20\tall\nchrM\t0\t10\tmt\n")
    assert [r["Mean depth"] for r in rows] == ["1.55", "50.00"]
    assert result.metrics["off_target_mean_depth"] == 0
    assert result.metrics["enrichment"] is None


@needs_samtools
@pytest.mark.parametrize(
    ("bed", "message"),
    [
        ("chr9\t0\t5\n", "contigs the alignment lacks: chr9"),
        ("chr1\t0\t25\n", "beyond the end of chr1"),
        ("chr1\tx\t5\n", "is not BED"),
    ],
)
def test_bad_bed_is_refused(tmp_path, bed, message):
    with pytest.raises(api.CommandFailed, match=message):
        _coverage(tmp_path, bed)


def test_by_excludes_bins_and_region(tmp_path):
    with pytest.raises(UsageError, match="--by can't be combined"):
        api.CalculateCoverageParams(
            input=tmp_path / "x.bam", by=tmp_path / "p.bed", bins=True
        )
