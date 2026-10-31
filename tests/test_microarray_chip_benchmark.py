import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.benchmark_microarray_chip import main
from tests.test_remote_benchmark import _in_called_region
from tests.test_remote_benchmark_chm13 import _in_native_called_region
from wgsextract_cli.commands.benchmark.chip import (
    CONSUMER_FORMATS,
    ChipCall,
    read_chip_export,
    score_chip,
)
from wgsextract_cli.commands.microarray import _MICROARRAY_TEMPLATE_MAP


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("23andMe_V5", "# header\nrs1\t20\t10\tGA\nrs2\t20\t11\t--\n"),
        (
            "Ancestry_V2",
            "rsid\tchromosome\tposition\tallele1\tallele2\n"
            "rs1\t20\t10\tG\tA\nrs2\t20\t11\t0\t0\n",
        ),
        (
            "MyHeritage_V1",
            "RSID,CHROMOSOME,POSITION,RESULT\n"
            '"rs1","20","10","GA"\n"rs2","20","11","--"\n',
        ),
        ("FTDNA_V3", "rs1,20,10,GA\nrs2,20,11,--\n"),
        ("Nucleus_V1", "rs1\t20\t10\tG/A\nrs2\t20\t11\t./.\n"),
    ],
)
def test_real_layouts_keep_nocalls(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / "export.txt.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    calls = read_chip_export(path, name)
    assert calls == {"rs1": ChipCall("20", "AG"), "rs2": ChipCall("20", None)}


def test_registry_covers_every_consumer_format() -> None:
    assert set(CONSUMER_FORMATS) == set(_MICROARRAY_TEMPLATE_MAP.values())


def test_duplicates_stay_quarantined_and_aliases_normalize(tmp_path: Path) -> None:
    path = tmp_path / "kit.txt"
    path.write_text(
        "rs1\t20\t1\tAA\nrs1\t20\t2\tGG\nrs1\t20\t3\tAA\n"
        "rs2\t23\t1\tA\nrs3\t26\t1\tC\nrs4\tXY\t1\tGG\n"
        "rs5\t1\t1\t--\nrs5\t1\t1\tAA\nrs6\t1\t1\tID\n",
        newline="\n",
    )
    calls = read_chip_export(path, "CombinedKit")
    assert calls["rs1"] == ChipCall("20", None, True)
    assert calls["rs5"] == ChipCall("1", None, True)
    assert calls["rs2"].chromosome == calls["rs4"].chromosome == "X"
    assert calls["rs3"].chromosome == "MT"
    assert calls["rs6"].genotype is None


def test_real_ftdna_unplaced_probes_remain_missing(tmp_path: Path) -> None:
    path = tmp_path / "ftdna.csv"
    path.write_text(
        "RSID,CHROMOSOME,POSITION,RESULT\n"
        "unplaced-probe,0,0,--\nrs1,20,0,AA\nrs2,20,10,AA\n",
        newline="\n",
    )
    assert read_chip_export(path, "FTDNA_V3") == {
        "unplaced-probe": ChipCall("0", None),
        "rs1": ChipCall("20", None),
        "rs2": ChipCall("20", "AA"),
    }


def test_malformed_genotypes_cannot_inflate_concordance(tmp_path: Path) -> None:
    path = tmp_path / "kit.tsv"
    malformed = ["A//G", "/AG", "AG/", "A/|G"]
    path.write_text(
        "".join(
            f"rs{number}\t20\t{number}\t{gt}\n"
            for number, gt in enumerate(malformed, 1)
        ),
        newline="\n",
    )
    calls = read_chip_export(path, "CombinedKit")
    truth = {marker: ChipCall("20", "AG") for marker in calls}
    result = score_chip(calls, truth)
    assert result.total.eligible == result.total.nocall == len(malformed)
    assert result.total.as_dict()["concordance"] is None


def test_incomplete_ancestry_pairs_are_not_haploid_calls(tmp_path: Path) -> None:
    path = tmp_path / "ancestry.tsv"
    path.write_text(
        "rs1\t23\t10\tA\t\nrs2\t23\t11\t\tA\nrs3\t23\t12\tAA\t\nrs4\t23\t13\tA\tA\n",
        newline="\n",
    )
    calls = read_chip_export(path, "Ancestry_V2")
    result = score_chip(calls, {marker: ChipCall("X", "AA") for marker in calls})
    assert result.total.eligible == 4
    assert result.total.nocall == 3
    assert result.total.called == result.total.concordant == 1
    assert result.total.as_dict()["call_rate"] == 0.25


def test_score_reports_missingness_mismatches_and_per_chromosome() -> None:
    truth = {
        "rs1": ChipCall("20", "AG"),
        "rs2": ChipCall("20", "AA"),
        "rs3": ChipCall("20", "GG"),
        "rs4": ChipCall("20", None),
        "rs5": ChipCall("X", "AA"),
        "rs6": ChipCall("20", "AA"),
        "absent_from_panel": ChipCall("1", "CC"),
    }
    kit = {
        "rs1": ChipCall("20", "AG"),
        "rs2": ChipCall("20", None),
        "rs3": ChipCall("20", "CC"),  # Never silently complement.
        "rs4": ChipCall("20", "AA"),
        "rs5": ChipCall("X", "A"),
        "rs6": ChipCall("21", "AA"),
        "absent_from_chip": ChipCall("1", "CC"),
    }
    result = score_chip(kit, truth)
    assert result.total.as_dict() == {
        "overlap": 6,
        "truth_nocall": 1,
        "chromosome_mismatch": 1,
        "eligible": 4,
        "called": 3,
        "concordant": 2,
        "discordant": 1,
        "nocall": 1,
        "call_rate": 0.75,
        "concordance": 2 / 3,
    }
    assert result.chromosomes["20"].nocall == 1
    assert result.chromosomes["X"].concordant == 1
    assert score_chip({}, truth).total.as_dict()["concordance"] is None
    assert "rs1" not in str(result.as_dict())


@pytest.mark.parametrize("line", ["rs1\t20\t1\n", "rs1\t29\t1\tAA\n"])
def test_malformed_data_fails_instead_of_inflating_concordance(
    tmp_path: Path, line: str
) -> None:
    path = tmp_path / "bad.txt"
    path.write_text(line, newline="\n")
    with pytest.raises(ValueError, match="Invalid chip"):
        read_chip_export(path, "CombinedKit")


def test_report_cli_contains_only_aggregate_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truth = tmp_path / "synthetic-truth.tsv"
    kit = tmp_path / "synthetic-kit.csv"
    report = tmp_path / "report.json"
    truth.write_text("rs999\t20\t10\tC/T\n", newline="\n")
    kit.write_text('"rs999","20","10","TC"\n', newline="\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark_microarray_chip.py",
            "--truth",
            str(truth),
            "--kit",
            f"MyHeritage_V1={kit}",
            "--output",
            str(report),
        ],
    )
    main()
    text = report.read_text()
    assert "rs999" not in text and "synthetic" not in text and "TC" not in text
    result = json.loads(text)["MyHeritage_V1"]
    assert result["total"]["concordance"] == 1
    assert result["chromosomes"]["20"]["called"] == 1


@pytest.mark.parametrize(
    ("hits", "inside"),
    [
        ([("chr20", 9_999_998, "+", 1)], False),
        ([("chr20", 9_999_999, "+", 1)], True),
        ([("chr20", 11_999_999, "+", 1)], True),
        ([("chr20", 12_000_000, "+", 1)], False),
        ([("chr20", 10_000_000, "-", 1)], False),
        ([("chr21", 10_000_000, "+", 1)], False),
        ([("chr20", 10_000_000, "+", 1)] * 2, False),
        ([], False),
    ],
)
def test_regional_eligibility_uses_unique_forward_mapping(
    hits: list[tuple[str, int, str, int]], inside: bool
) -> None:
    lifter = SimpleNamespace(convert_coordinate=lambda chrom, pos: hits)
    assert _in_called_region("20", "11000000", lifter) is inside


@pytest.mark.parametrize(
    ("status", "chrom", "position", "inside"),
    [
        ("mapped", "chr20", 9_999_999, False),
        ("mapped", "chr20", 10_000_000, True),
        ("mapped", "chr20", 12_000_000, True),
        ("mapped", "chr20", 12_000_001, False),
        ("mapped", "chr21", 11_000_000, False),
        ("mapped", "chr20", None, False),
        ("nonreciprocal", "chr20", 11_000_000, False),
        ("collision", "chr20", 11_000_000, False),
    ],
)
def test_native_eligibility_uses_only_valid_mapped_coordinates(
    status: str, chrom: str, position: int | None, inside: bool
) -> None:
    site = SimpleNamespace(status=status, target_chrom=chrom, target_pos=position)
    mapping = SimpleNamespace(source_lookup=lambda chrom, pos: site)
    assert _in_native_called_region("20", "11000000", mapping) is inside
    mapping = SimpleNamespace(source_lookup=lambda chrom, pos: None)
    assert not _in_native_called_region("20", "11000000", mapping)
