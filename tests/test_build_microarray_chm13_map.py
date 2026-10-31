import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import scripts.build_microarray_chm13_map as builder
from scripts.build_microarray_chm13_map import (
    align_mitochondria,
    project_sites,
    write_site_map,
)
from wgsextract_cli.core.microarray_site_map import SiteProjection, load_site_map
from wgsextract_cli.core.utils import WGSExtractError


@pytest.mark.skipif(shutil.which("samtools") is None, reason="samtools required")
def test_indexed_reference_windows_cross_boundaries_and_verify_actual_sequence(
    tmp_path: Path,
) -> None:
    sequences = dict.fromkeys(builder._CHROMS, "ACGT")
    sequences["chr1"] = "ACGT" * 250001
    fasta = tmp_path / "input.fa"
    fasta.write_text(
        "".join(f">{chrom}\n{sequence}\n" for chrom, sequence in sequences.items())
    )
    subprocess.run(["samtools", "faidx", str(fasta)], check=True)
    dictionary = tmp_path / "input.dict"
    dictionary.write_text(
        "".join(
            f"@SQ\tSN:{chrom}\tLN:{len(sequence)}\t"
            f"M5:{hashlib.md5(sequence.encode()).hexdigest()}\n"
            for chrom, sequence in sequences.items()
        )
    )
    reference = builder.ReferenceSequences(fasta, dictionary)
    sequence = reference.indexed_sequence("chr1")
    assert len(sequence) == len(sequences["chr1"])
    for index in (0, 999999, 1000000, 1000003, 0):
        assert sequence[index] == sequences["chr1"][index]
    with pytest.raises(IndexError):
        _ = sequence[len(sequence)]
    fasta.write_text(fasta.read_text().replace("ACGT", "TCGT", 1))
    changed = builder.ReferenceSequences(fasta, dictionary)
    with pytest.raises(ValueError, match="sequence/dictionary mismatch"):
        changed.indexed_sequence("chr1")


class _Lifter:
    def __init__(
        self, hits: dict[tuple[str, int], list[tuple[str, int, str, int]]]
    ) -> None:
        self.hits = hits

    def convert_coordinate(
        self, chromosome: str, position: int
    ) -> list[tuple[str, int, str, int]] | None:
        return self.hits.get((chromosome, position))


def test_full_union_includes_aadr_numeric_and_sex_chromosome_sites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "markers.txt"
    template.write_text("marker-only fixture\n")
    monkeypatch.setattr(builder, "_FORMATS", ["Test"])
    monkeypatch.setattr(builder, "_resolve_templates_roots", lambda roots: roots)
    monkeypatch.setattr(
        builder,
        "_template_paths",
        lambda _roots: iter([("Test", False, [str(template)])]),
    )
    monkeypatch.setattr(
        builder, "template_rows", lambda *_args: iter([("rs1", "1", "10")])
    )
    monkeypatch.setattr(builder, "LiftOver", lambda _path: _Lifter({}))
    from wgsextract_cli.core.microarray_aadr import AADRMarker

    monkeypatch.setattr(
        builder,
        "aadr_markers",
        lambda _panel: iter(
            [
                AADRMarker("aadr1", "1", "0", 20, "A", "G", True),
                AADRMarker("aadrX", "23", "0", 30, "A", "G", False),
                AADRMarker("knownIndel", "1", "0", 40, "A", "AT", False),
            ]
        ),
    )
    monkeypatch.setattr(builder, "_MARKERS", template)
    markers, provenance = builder.collect_markers(
        [str(tmp_path)], tmp_path / "unused.chain", {"chr1": 100, "chrX": 100}
    )
    assert markers == {
        ("chr1", 10): "rs1",
        ("chr1", 20): "aadr1",
        ("chrX", 30): "aadrX",
        ("chr1", 40): "knownIndel",
    }
    assert provenance["legacy_sites"] == 1
    assert provenance["complete_sites"] == 4
    alleles = provenance["source_alleles"]
    assert alleles["known_marker_rows"] == 3
    assert alleles["non_snv_marker_rows"] == 1
    assert alleles["non_snv_sites"] == [("chr1", 40)]
    assert alleles["non_snv_ids"] == ["knownIndel"]


def test_mitochondrial_bridge_tracks_reference_substitution() -> None:
    bridge = align_mitochondria("ACGT", "ACAT", band=2)
    assert bridge.edit_distance == 1
    assert bridge.source_to_target == (0, 1, 2, 3, 4)
    assert bridge.target_to_source == (0, 1, 2, 3, 4)
    assert bridge.aligned_source == "ACGT"
    assert bridge.aligned_target == "ACAT"


def test_mitochondrial_bridge_quarantines_all_optimal_homopolymer_placements() -> None:
    bridge = align_mitochondria("CAAAG", "CAAG", band=2)
    assert bridge.edit_distance == 1
    assert bridge.source_to_target == (0, 1, 0, 0, 0, 4)
    assert bridge.target_to_source == (0, 1, 0, 0, 5)
    assert bridge.aligned_source.replace("-", "") == "CAAAG"
    assert bridge.aligned_target.replace("-", "") == "CAAG"
    assert align_mitochondria("CAAAG", "CAAG", band=2) == bridge


def test_mitochondrial_bridge_handles_a_unique_gap_and_both_boundaries() -> None:
    bridge = align_mitochondria("ACGT", "ACT", band=2)
    assert bridge.source_to_target == (0, 1, 2, 0, 3)
    assert bridge.target_to_source == (0, 1, 2, 4)
    reverse = align_mitochondria("ACT", "ACGT", band=2)
    assert reverse.source_to_target == bridge.target_to_source
    assert reverse.target_to_source == bridge.source_to_target
    terminal = align_mitochondria("ACGT", "CG", band=2)
    assert terminal.source_to_target == (0, 0, 1, 2, 0)
    assert terminal.target_to_source == (0, 2, 3)


def test_mitochondrial_bridge_does_not_claim_global_optimality_with_a_narrow_band() -> (
    None
):
    with pytest.raises(ValueError, match="globally complete"):
        align_mitochondria("AAAAAA", "TTTTTT", band=2)
    identity = align_mitochondria("ACGT", "ACGT", band=0)
    assert identity.source_to_target == (0, 1, 2, 3, 4)


def test_mitochondrial_bridge_quarantines_unknown_reference_bases() -> None:
    bridge = align_mitochondria("ACNT", "ACGT", band=2)
    assert bridge.edit_distance == 1
    assert bridge.source_to_target == (0, 1, 2, 0, 4)
    assert bridge.target_to_source == (0, 1, 2, 0, 4)
    both_unknown = align_mitochondria("ACNT", "ACNT", band=2)
    assert both_unknown.edit_distance == 1
    assert both_unknown.source_to_target == (0, 1, 2, 0, 4)


def test_mitochondrial_bridge_restores_native_coordinates_across_circular_origin() -> (
    None
):
    bridge = align_mitochondria("ACGTTGCA", "TTGCAACG", target_rotation=5, band=0)
    assert bridge.edit_distance == 0
    assert bridge.source_to_target == (0, 6, 7, 8, 1, 2, 3, 4, 5)
    assert bridge.target_to_source == (0, 4, 5, 6, 7, 8, 1, 2, 3)
    assert bridge.original_target() == "TTGCAACG"
    assert bridge.aligned_target == "ACGTTGCA"
    for source, target in enumerate(bridge.source_to_target[1:], 1):
        assert bridge.target_to_source[target] == source


@pytest.mark.parametrize("source,target", [("", "AC"), ("AC", "AR")])
def test_mitochondrial_bridge_rejects_unknown_bases(source: str, target: str) -> None:
    with pytest.raises(ValueError, match="nonempty A/C/G/T/N"):
        align_mitochondria(source, target)


def test_compact_writer_is_reproducible_and_preserves_static_no_calls(
    tmp_path: Path,
) -> None:
    sites = [
        SiteProjection("chr1", 10, "chr1", 20, False, "mapped", "ref_equal"),
        SiteProjection("chr1", 20, "chr1", 5, True, "mapped", "ref_different"),
        SiteProjection("chr1", 30, None, None, False, "collision", "ref_unknown"),
    ]
    first = write_site_map(sites, {"chr1": 100}, {"chr1": 100}, tmp_path / "first", {})
    second = write_site_map(
        sites, {"chr1": 100}, {"chr1": 100}, tmp_path / "second", {}
    )
    assert first.read_bytes() == second.read_bytes()
    filename = json.loads(first.read_text())["file"]
    assert (first.parent / filename).read_bytes() == (
        second.parent / filename
    ).read_bytes()
    mapping = load_site_map(first)
    assert list(mapping.source_sites()) == sites
    assert list(mapping.native_sites()) == [sites[1], sites[0]]


def test_compact_writer_does_not_publish_a_colliding_or_partial_map(
    tmp_path: Path,
) -> None:
    sites = [
        SiteProjection("chr1", 10, "chr1", 20, False, "mapped", "ref_equal"),
        SiteProjection("chr1", 20, "chr1", 20, False, "mapped", "ref_equal"),
    ]
    with pytest.raises(WGSExtractError, match="Duplicate or unsorted native"):
        write_site_map(sites, {"chr1": 100}, {"chr1": 100}, tmp_path, {})
    assert list(tmp_path.iterdir()) == []


def test_static_collision_survives_multihit_partner_and_contig_filter(
    tmp_path: Path,
) -> None:
    forward = _Lifter(
        {
            ("chr1", 0): [("chr1", 1, "+", 1)],
            ("chr2", 0): [("chr1", 1, "+", 1), ("chr1", 2, "+", 1)],
        }
    )
    reverse = _Lifter({("chr1", 1): [("chr1", 0, "+", 1)]})
    rows, counts = project_sites(
        [("chr1", 1), ("chr2", 1)],
        forward,
        reverse,
        lambda _chrom: "A",
        lambda _chrom: "AAA",
        align_mitochondria("A", "A"),
    )
    assert counts["collision"] == 2
    selected = [site for site in rows if site.source_chrom == "chr1"]
    path = write_site_map(selected, {"chr1": 1}, {"chr1": 3}, tmp_path, {})
    mapping = load_site_map(path)
    remaining = mapping.source_lookup("chr1", 1)
    assert remaining is not None
    assert remaining.status == "collision"
    assert list(mapping.native_range("chr1", 2, 2)) == []


def test_ref_diagnostic_complements_inversion_without_masking_reference_difference() -> (
    None
):
    forward = _Lifter(
        {
            ("chr1", 0): [("chr1", 0, "-", 1)],
            ("chr1", 1): [("chr1", 2, "+", 1)],
        }
    )
    reverse = _Lifter(
        {
            ("chr1", 0): [("chr1", 0, "-", 1)],
            ("chr1", 2): [("chr1", 1, "+", 1)],
        }
    )
    rows, counts = project_sites(
        [("chr1", 1), ("chr1", 2)],
        forward,
        reverse,
        lambda _chrom: "AC",
        lambda _chrom: "TTA",
        align_mitochondria("A", "A"),
    )
    assert [(site.status, site.ref_status) for site in rows] == [
        ("mapped", "ref_equal"),
        ("mapped", "ref_different"),
    ]
    assert counts["mapped"] == 2
    assert counts["ref_different"] == 1


def test_mt_sites_use_global_bridge_instead_of_nuclear_chain_or_positional_identity() -> (
    None
):
    rows, counts = project_sites(
        [("chrM", pos) for pos in range(1, 6)],
        _Lifter({}),
        _Lifter({}),
        lambda _chrom: "CAAAG",
        lambda _chrom: "CAAG",
        align_mitochondria("CAAAG", "CAAG"),
    )
    assert counts["mt_gap"] == 3
    assert rows[-1].target_pos == 4
    assert rows[-1].source_pos == 5
    assert rows[-1].ref_status == "ref_equal"


def test_grch38_crosscheck_never_overrides_direct_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifters = {
        "hg19ToHg38.over.chain.gz": _Lifter(
            {
                ("chr1", 0): [("chr1", 10, "+", 1)],
                ("chr1", 1): [("chr1", 11, "+", 1)],
            }
        ),
        "hg38ToHg19.over.chain.gz": _Lifter(
            {
                ("chr1", 10): [("chr1", 0, "+", 1)],
                ("chr1", 11): [("chr1", 1, "+", 1)],
            }
        ),
        "grch38-chm13v2.chain": _Lifter(
            {
                ("chr1", 10): [("chr1", 100, "+", 1)],
                ("chr1", 11): [("chr1", 998, "+", 1)],
            }
        ),
        "chm13v2-grch38.chain": _Lifter(
            {
                ("chr1", 100): [("chr1", 10, "+", 1)],
                ("chr1", 998): [("chr1", 11, "+", 1)],
            }
        ),
    }
    for name in lifters:
        (tmp_path / name).write_text("test-only placeholder", encoding="utf-8")
    monkeypatch.setattr(builder, "LiftOver", lambda path: lifters[Path(path).name])
    sites = [
        SiteProjection("chr1", 1, "chr1", 101, False, "mapped", "ref_equal"),
        SiteProjection("chr1", 2, "chr1", 102, False, "mapped", "ref_equal"),
    ]
    audit = builder.audit_grch38(sites, tmp_path, tmp_path)
    assert audit["counts"] == {
        "nuclear_sites": 2,
        "bridge_coordinate_disagreement": 1,
        "GRCh38_static_collision_sites": 0,
        "eligible_sites": 1,
    }
    assert [site.target_pos for site in sites] == [101, 102]
    with gzip.open(tmp_path / "validation-grch38-reciprocal.tsv.gz", "rt") as source:
        lines = source.read().splitlines()
    assert lines[1:] == ["chr1\t1\tchr1\t11\t0\tchr1\t101\t0"]
