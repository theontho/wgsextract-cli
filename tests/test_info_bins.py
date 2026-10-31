import csv
import io
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli.commands.info.bins import count_depth_bins
from wgsextract_cli.core.utils import WGSExtractError, run_command

needs_samtools = needs_tools("samtools")


def test_bin_boundaries_and_inferred_uncovered_contigs():
    depths = [0, 1, 3, 4, 7, 8, 100]
    rows = [f"chrY\t{i + 1}\t{depth}\n" for i, depth in enumerate(depths)]
    assert count_depth_bins(iter(rows), {"chrY": 10, "chrM": 3}) == {
        "chrY": [4, 2, 2, 2],
        "chrM": [3, 0, 0, 0],
    }
    with pytest.raises(WGSExtractError, match="Unexpected"):
        count_depth_bins(["bad\n"], {"chrY": 10})


@needs_samtools
@pytest.mark.parametrize("region", [None, "Y:2-5"])
@pytest.mark.parametrize("cram", [False, True])
def test_bins_real_samtools_default_filters_and_region(
    tmp_path: Path, capsys, region, cram
):
    lines = ["@SQ\tSN:chrY\tLN:10", "@SQ\tSN:chrM\tLN:3"]
    for position, depth in enumerate([0, 1, 3, 4, 7, 8, 9, 0, 0, 0], 1):
        for read in range(depth):
            lines.append(
                f"p{position}_{read}\t0\tchrY\t{position}\t0\t1M\t*\t0\t0\tA\t!"
            )
    for flag in [256, 512, 1024]:
        lines.append(f"filtered{flag}\t{flag}\tchrY\t1\t60\t1M\t*\t0\t0\tA\tI")
    # Deletions do not increase depth, and both overlapping mates do.
    lines += [
        "deletion\t0\tchrY\t9\t60\t1M1D\t*\t0\t0\tA\tI",
        "pair\t65\tchrY\t8\t60\t1M\t=\t8\t1\tA\tI",
        "pair\t129\tchrY\t8\t60\t1M\t=\t8\t-1\tA\tI",
    ]
    bam = sam_to_bam(lines, tmp_path / "tiny.bam")
    reference = None
    if cram:
        reference = write_fasta(
            tmp_path / "reference.fa", {"chrY": "A" * 10, "chrM": "AAA"}
        )
        encoded = tmp_path / "tiny.cram"
        run_command(["samtools", "view", "-C", "-T", reference, "-o", encoded, bam])
        run_command(["samtools", "index", encoded])
        bam = str(encoded)
    args = [
        "info",
        "calculate-coverage",
        "--bins",
        "--input",
        bam,
        "--outdir",
        str(tmp_path / "o"),
        "--threads",
        "2",
    ]
    if region:
        args += ["--region", region]
    if reference:
        args += ["--ref", str(reference)]
    run_wgsextract(*args)
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out), delimiter="\t"))
    y = rows[0]
    assert [int(y[f"Depth {b} bases"]) for b in ("0", "1-3", "4-7", "8+")] == (
        [0, 2, 2, 0] if region else [2, 4, 2, 2]
    )
    assert sum(
        float(y[f"Depth {b} fraction"]) for b in ("0", "1-3", "4-7", "8+")
    ) == pytest.approx(1)
    if not region:
        assert rows[1]["Depth 0 bases"] == "3"
    assert len(list((tmp_path / "o").glob("*_coverage_bins.tsv"))) == 1
    assert not list((tmp_path / "o").glob("*_coverage.tsv"))


@needs_samtools
@pytest.mark.parametrize("directory_ref", [False, True])
def test_cram_bins_require_a_usable_explicit_reference(
    tmp_path, monkeypatch, directory_ref
):
    from wgsextract_cli.commands.info import bins

    reference = write_fasta(tmp_path / "reference.fa", {"chrY": "AAAA"})
    bam = sam_to_bam(
        ["@SQ\tSN:chrY\tLN:4", "read\t0\tchrY\t1\t60\t1M\t*\t0\t0\tA\tI"],
        tmp_path / "sample.bam",
    )
    cram = tmp_path / "sample.cram"
    run_command(["samtools", "view", "-C", "-T", reference, "-o", cram, bam])
    launch = Mock(
        side_effect=AssertionError("depth must not decode without a reference")
    )
    monkeypatch.setattr(bins, "popen", launch)
    args = [
        "info",
        "calculate-coverage",
        "--bins",
        "--input",
        str(cram),
        "--outdir",
        str(tmp_path / "o"),
    ]
    if directory_ref:
        empty_library = tmp_path / "empty-library"
        empty_library.mkdir()
        args += ["--ref", str(empty_library)]
    with pytest.raises(WGSExtractError, match="--ref required"):
        run_wgsextract(*args)
    launch.assert_not_called()


@needs_samtools
@pytest.mark.parametrize("region", ["MT", "MT:2-5"])
def test_bins_honor_exact_name_before_mitochondrial_aliases(tmp_path, capsys, region):
    bam = sam_to_bam(
        [
            "@SQ\tSN:chrM\tLN:6",
            "@SQ\tSN:MT\tLN:10",
            "prefixed\t0\tchrM\t2\t60\t1M\t*\t0\t0\tA\tI",
            "exact1\t0\tMT\t2\t60\t1M\t*\t0\t0\tA\tI",
            "exact2\t0\tMT\t4\t60\t1M\t*\t0\t0\tA\tI",
        ],
        tmp_path / "dual.bam",
    )
    run_wgsextract(
        "info",
        "calculate-coverage",
        "--bins",
        "--input",
        bam,
        "--outdir",
        str(tmp_path / "o"),
        "--region",
        region,
    )
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out), delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["Seq Name"] == "MT"
    assert rows[0]["Model Len"] == ("10" if region == "MT" else "4")
    assert rows[0]["Depth 1-3 bases"] == "2"
