from pathlib import Path

import pytest

from scripts.build_microarray_chm13_map import write_site_map
from wgsextract_cli.commands._microarray_chm13 import _regions, _selected
from wgsextract_cli.core.microarray_chm13_calls import (
    parse_native_calls,
    project_native_calls,
)
from wgsextract_cli.core.microarray_identity import MicroarrayIdentity
from wgsextract_cli.core.microarray_quality import MicroarrayQuality
from wgsextract_cli.core.microarray_site_map import (
    SiteMap,
    SiteProjection,
    load_site_map,
)


@pytest.fixture
def mapping(tmp_path: Path) -> SiteMap:
    return load_site_map(
        write_site_map(
            [
                SiteProjection("chr1", 10, "chr1", 20, True, "mapped", "ref_different"),
                SiteProjection("chr1", 11, "chr1", 21, False, "mapped", "ref_equal"),
                SiteProjection("chrM", 10, None, None, False, "mt_gap", "ref_unknown"),
                SiteProjection(
                    "chrM", 11, "chrM", 12, False, "mapped", "ref_different"
                ),
            ],
            {"chr1": 100, "chrM": 100},
            {"chr1": 100, "chrM": 100},
            tmp_path,
            {},
        )
    )


def _ids(mapping: SiteMap) -> dict[tuple[str, int], str]:
    return {
        (site.source_chrom, site.source_pos): f"rs{index}"
        for index, site in enumerate(mapping.source_sites(), 1)
    }


def test_inverted_and_mitochondrial_calls_are_projected_not_reference_rewritten(
    mapping: SiteMap,
) -> None:
    ids = _ids(mapping)
    quality, identity = MicroarrayQuality(), MicroarrayIdentity()
    calls, represented = parse_native_calls(
        [
            "chr1\t20\t20\tA\tG\t0/1\t10\t30\tPASS\n",
            "MT\t12\t12\tG\tA\t0\t10\t30\tPASS\n",
        ],
        mapping,
        ids,
        quality,
        identity,
    )
    projected = list(
        project_native_calls(
            mapping.source_sites(),
            ids,
            calls,
            quality=quality,
            identity=identity,
            is_vcf=False,
            represented=represented,
            native_bases={},
        )
    )
    assert [(call.genotype, call.reason) for call in projected] == [
        ("CT", "called"),
        ("--", "no_reads"),
        ("--", "mt_gap"),
        ("GG", "called"),
    ]


@pytest.mark.parametrize(
    "first,second,reason",
    [
        ("A\tG\t0/1\t10\t30", "A\tG\t0/0\t10\t30", "conflicting_duplicate"),
        ("A\tAT\t0/0\t10\t30", "A\tG\t0/0\t10\t30", "unsupported_indel"),
        ("A\tG\t0/0\t10\t30", "A\tAT\t0/0\t10\t30", "unsupported_indel"),
        ("A\tG\t0/1\t1\t30", "A\tG\t0/1\t10\t30", "low_dp"),
        ("A\tG\t./1\t10\t30", "A\tG\t0/1\t10\t30", "missing_gt"),
    ],
)
def test_duplicate_rejection_is_sticky(
    mapping: SiteMap, first: str, second: str, reason: str
) -> None:
    calls, _ = parse_native_calls(
        [f"chr1\t20\t20\t{record}\tPASS\n" for record in (first, second)],
        mapping,
        _ids(mapping),
        MicroarrayQuality(min_dp=5),
        MicroarrayIdentity(),
    )
    assert calls[("chr1", 10)] == ("--", reason)


def test_deletion_starting_before_a_target_quarantines_all_overlapping_sites(
    mapping: SiteMap,
) -> None:
    calls, _ = parse_native_calls(
        ["chr1\t19\t21\tAAA\tA\t0/1\t10\t30\tPASS\n"],
        mapping,
        _ids(mapping),
        MicroarrayQuality(),
        MicroarrayIdentity(),
    )
    assert calls == {
        ("chr1", 10): ("--", "unsupported_indel"),
        ("chr1", 11): ("--", "unsupported_indel"),
    }


def test_vcf_reference_fill_uses_native_reference_then_complements_once(
    mapping: SiteMap,
) -> None:
    rows = list(
        project_native_calls(
            mapping.source_sites(),
            _ids(mapping),
            {},
            quality=MicroarrayQuality(),
            identity=MicroarrayIdentity(),
            is_vcf=True,
            represented={"chr1"},
            native_bases={("chr1", 20): "G", ("chr1", 21): "C"},
        )
    )
    assert [(call.genotype, call.reason) for call in rows] == [
        ("CC", "assumed_hom_ref"),
        ("CC", "assumed_hom_ref"),
        ("--", "mt_gap"),
        ("--", "absent_contig"),
    ]


def test_whole_mitochondrial_region_retains_explicit_bridge_no_call(
    mapping: SiteMap,
) -> None:
    site = mapping.source_lookup("chrM", 10)
    assert site is not None
    assert _selected(site, [("chrM", 1, 16569)])
    assert not _selected(site, [("chr1", 1, 100)])
    assert not _selected(site, [("chrM", 2, 100)])


@pytest.mark.parametrize(
    "region,expected",
    [
        ("20:10-", [("chr20", 10, 0xFFFFFFFF)]),
        ("20:10", [("chr20", 10, 10)]),
        ("M", [("chrM", 1, 0xFFFFFFFF)]),
        ("X:-20", [("chrX", 1, 20)]),
    ],
)
def test_native_region_keeps_open_ended_and_single_position_semantics(
    region: str, expected: list[tuple[str, int, int]]
) -> None:
    assert _regions(region) == expected
