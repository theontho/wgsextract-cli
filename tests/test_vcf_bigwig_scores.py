from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools
from tests.test_vcf_annotations import _vcf
from wgsextract_cli.commands.vcf import bigwig_scores


@needs_tools("bcftools", "bgzip", "tabix")
@pytest.mark.parametrize("use_numpy", [False, True])
def test_windowed_scores_match_exact_stats_across_gaps_and_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_numpy: bool
) -> None:
    pybigwig = pytest.importorskip("pyBigWig")
    if use_numpy and not pybigwig.numpy:
        pytest.skip("pyBigWig was compiled without NumPy support")
    monkeypatch.setattr(pybigwig, "numpy", int(use_numpy))
    path = str(tmp_path / "scores.bw")
    with pybigwig.open(path, "w") as destination:
        destination.addHeader([("1", 32), ("2", 16)])
        destination.addEntries(
            ["1"] * 6,
            [0, 1, 7, 8, 14, 29],
            ends=[1, 2, 8, 9, 15, 30],
            values=[-4.0, -2.0, 7.0, 1.0, -0.5, 3.0],
        )
        destination.addEntries("2", 4, values=[-7.0], span=1, step=1)
    sample = _vcf(
        tmp_path / "sample.vcf.gz",
        [
            "1 snv A C . PASS . GT 0/1",
            "2 gap ACGT A . PASS . GT 0/1",
            "6 block A <*> . PASS . GT 0/0",
            "8 boundary AA A . PASS . GT 0/1",
            "9 multi A C,T . PASS . GT 1/2",
            "12 sparse AAAA A . PASS . GT 0/1",
            f"13 long {'A' * 12} A . PASS . GT 0/1",
            "31 past_end AAA A . PASS . GT 0/1",
        ],
        contig="chr1",
        length=32,
    )
    monkeypatch.setattr(bigwig_scores, "_WINDOW_SIZE", 8)
    with pybigwig.open(path) as source:
        calls = []

        class Tracked:
            chroms = source.chroms

            def stats(self, *args, **kwargs):
                calls.append(("stats", args))
                return source.stats(*args, **kwargs)

            def values(self, *args, **kwargs):
                calls.append(("values", args))
                return source.values(*args, **kwargs)

        actual = list(bigwig_scores._score_rows(sample, Tracked(), "GERP"))
        expected = []
        for pos, ref, alts in (
            (1, "A", ["C"]),
            (2, "ACGT", ["A"]),
            (8, "AA", ["A"]),
            (9, "A", ["C", "T"]),
            (12, "AAAA", ["A"]),
            (13, "A" * 12, ["A"]),
            (31, "AAA", ["A"]),
        ):
            score = source.stats(
                "1", pos - 1, min(pos - 1 + len(ref), 32), type="max", exact=True
            )[0]
            if score is not None:
                expected.extend(
                    f"chr1\t{pos}\t{ref}\t{alt}\t{score:.4g}\n" for alt in alts
                )
        assert actual == expected
        assert [args for method, args in calls if method == "stats"] == [("1", 12, 24)]
        assert len([method for method, _args in calls if method == "values"]) == 4
        window = bigwig_scores._ScoreWindow(source, source.chroms())
        assert window.maximum("1", 0, 1) == -4.0
        assert window.maximum("2", 4, 5) == -7.0
