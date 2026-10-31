import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

import pytest
from scripts.build_microarray_chm13_map import write_site_map
from wgsextract_cli.core.microarray_chm13 import CHM13Catalogue, ReferenceContig
from wgsextract_cli.core.microarray_chm13_indels import (
    FlankedSite,
    call_chm13_indels,
    expected_ploidy,
    flank_evidence,
    flanked_sites,
    likelihood_gq,
    native_probe_sites,
    read_haplotype,
    supported_call,
)
from wgsextract_cli.core.microarray_indel_probes import IndelProbe, IndelSite
from wgsextract_cli.core.microarray_site_map import SiteProjection, load_site_map

from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta

LEFT = "ACCGTGACTGCATGC"
RIGHT = "GACTACGTAGCTGCA"
SITE = FlankedSite(IndelSite("chr1", 100, "CGT", "C", "I"), LEFT, RIGHT)


def _read(name, cigar, sequence, *, start=85, flag=0, mapq=60):
    return [
        name,
        str(flag),
        "chr1",
        str(start),
        str(mapq),
        cigar,
        "*",
        "0",
        "0",
        sequence,
        "I" * len(sequence),
    ]


def test_complete_flanks_and_repeat_equivalent_deletions_not_anchor_bases():
    ref = LEFT + "CGT" + RIGHT
    alt = LEFT + "C" + RIGHT
    assert read_haplotype(_read("R", "33M", ref), SITE) == "ref"
    assert read_haplotype(_read("A", "16M2D15M", alt), SITE) == "alt"
    assert read_haplotype(_read("partial", "20M", ref[:20]), SITE) is None
    assert read_haplotype(_read("wrong", "33M", "T" + ref[1:]), SITE) is None
    assert read_haplotype(_read("other", "16M1D16M", LEFT + "CT" + RIGHT), SITE) == "!"
    assert (
        read_haplotype(_read("spanning", "10M30D15M", LEFT[:10] + RIGHT), SITE) == "!"
    )
    assert read_haplotype(_read("skip", "16M2N15M", alt), SITE) == "!"
    low = _read("low", "33M", ref)
    low[10] = "!" + low[10][1:]
    assert read_haplotype(low, SITE) is None


def test_fragment_deduplication_conflicts_and_read_quality_are_fail_closed():
    ref, alt = LEFT + "CGT" + RIGHT, LEFT + "C" + RIGHT
    rows = [
        _read("pair", "33M", ref),
        _read("pair", "33M", ref),
        _read("alternate", "16M2D15M", alt),
        _read("secondary", "16M2D15M", alt, flag=256),
        _read("duplicate", "16M2D15M", alt, flag=1024),
        _read("supplementary", "16M2D15M", alt, flag=2048),
        _read("lowmapq", "16M2D15M", alt, mapq=19),
    ]
    evidence = flank_evidence(("\t".join(row) for row in rows), {"P": SITE})
    assert evidence["P"] == Counter(ref=1, alt=1)
    rows.append(_read("pair", "16M2D15M", alt))
    assert (
        flank_evidence(("\t".join(row) for row in rows), {"P": SITE})["P"]["ambiguous"]
        == 1
    )


@pytest.mark.parametrize(
    ("cigar", "sequence"),
    [
        ("14M4D15M", LEFT[:14] + RIGHT),
        ("16M4D13M", LEFT + "C" + RIGHT[2:]),
    ],
)
def test_contained_core_overlapping_deletions_quarantine_reference_calls(
    cigar, sequence
):
    deletion = _read("contained", cigar, sequence)
    assert read_haplotype(deletion, SITE) == "!"
    rows = [_read(f"ref{i}", "33M", LEFT + "CGT" + RIGHT) for i in range(4)]
    rows.append(deletion)
    evidence = flank_evidence(("\t".join(row) for row in rows), {"P": SITE})["P"]
    assert supported_call("II", SITE.site, evidence, 2) == "--"


def test_contained_repeat_equivalent_deletion_requires_complete_haplotype():
    probe = FlankedSite(IndelSite("chr1", 100, "CAA", "C", "I"), LEFT, "A" + "T" * 14)
    assert (
        read_haplotype(_read("equivalent", "17M2D14M", LEFT + "CA" + "T" * 14), probe)
        == "alt"
    )


@needs_tools("samtools")
@pytest.mark.parametrize("insertion", [False, True])
@pytest.mark.parametrize("motif", ["A", "AT"])
def test_long_repeat_requires_spanning_unique_outer_flanks(tmp_path, insertion, motif):
    repeat = motif * 30
    sequence = "T" * 84 + LEFT + "G" + repeat + RIGHT + "T" * 100
    fasta = write_fasta(tmp_path / "repeat.fa", {"chr1": sequence})
    site = (
        IndelSite("chr1", 100, "G", "G" + motif, "D")
        if insertion
        else IndelSite("chr1", 100, "G" + motif, "G", "I")
    )
    probe = flanked_sites({"P": site}, str(fasta), tmp_path)["P"]
    assert probe.start == 85 and probe.end == 115 + len(repeat)
    partial = _read("partial", "46M", sequence[79:125], start=80)
    assert read_haplotype(partial, probe) is None
    alt = LEFT + "G" + motif * (31 if insertion else 29) + RIGHT
    size = len(motif)
    left_cigar = (
        f"16M{size}I{len(repeat) + 15}M"
        if insertion
        else f"16M{size}D{len(repeat) - size + 15}M"
    )
    right_cigar = (
        f"{16 + len(repeat)}M{size}I15M"
        if insertion
        else f"{16 + len(repeat) - size}M{size}D15M"
    )
    assert read_haplotype(_read("left", left_cigar, alt), probe) == "alt"
    assert read_haplotype(_read("right", right_cigar, alt), probe) == "alt"
    assert (
        read_haplotype(
            _read(
                "repeat-overlap",
                f"40M{len(repeat) - 20}D11M",
                LEFT + "G" + repeat[:24] + RIGHT[4:],
            ),
            probe,
        )
        == "!"
    )
    rows = [
        _read(f"partial{i}", "46M", sequence[79:125], start=80) for i in range(3)
    ] + [_read(f"alt{i}", right_cigar, alt) for i in range(3)]
    evidence = flank_evidence(("\t".join(row) for row in rows), {"P": probe})["P"]
    assert evidence == Counter(alt=3)
    assert supported_call("DI", site, evidence, 2) == "--"
    assert supported_call("II" if insertion else "DD", site, evidence, 2) != "--"
    rows += [
        _read(f"ref{i}", f"{31 + len(repeat)}M", LEFT + "G" + repeat + RIGHT)
        for i in range(3)
    ]
    evidence = flank_evidence(("\t".join(row) for row in rows), {"P": probe})["P"]
    assert supported_call("DI", site, evidence, 2) == "DI"


@needs_tools("samtools")
@pytest.mark.parametrize("insertion", [False, True])
@pytest.mark.parametrize("unbounded", [False, True])
def test_unresolved_repeat_or_nonunique_outer_anchor_no_calls(
    tmp_path, insertion, unbounded
):
    sequence = (
        "T" * 84
        + LEFT
        + "G"
        + "A" * (5000 if unbounded else 30)
        + RIGHT
        + "T" * 20
        + RIGHT
    )
    fasta = write_fasta(tmp_path / "uncertain.fa", {"chr1": sequence})
    site = (
        IndelSite("chr1", 100, "G", "GA", "D")
        if insertion
        else IndelSite("chr1", 100, "GA", "G", "I")
    )
    assert flanked_sites({"P": site}, str(fasta), tmp_path) == {}


@pytest.mark.parametrize(
    ("gt", "pl", "expected"),
    [
        ("0/0", "0,60,214", "60"),
        ("0/1", "70,0,80", "70"),
        ("1", "50,0", "50"),
        ("1/1", "0,10,20", "0"),
        ("0/0", ".", "."),
        ("0/0", "0,40", "."),
        ("0/2", "0,10,20", "."),
    ],
)
def test_forced_hom_ref_gq_requires_complete_biallelic_likelihoods(gt, pl, expected):
    assert likelihood_gq(gt, pl) == expected


@pytest.mark.parametrize(
    ("gt", "evidence", "ploidy", "expected"),
    [
        ("II", Counter(ref=4), 2, "II"),
        ("DI", Counter(ref=2, alt=2), 2, "DI"),
        ("DD", Counter(alt=4), 2, "DD"),
        ("D", Counter(alt=4), 1, "D"),
        ("DD", Counter(alt=4), 1, "--"),
        ("D", Counter(alt=4), 0, "--"),
        ("DI", Counter(ref=5, alt=1), 2, "--"),
        ("II", Counter(ref=4, alt=1), 2, "--"),
        ("II", Counter(ref=4, ambiguous=1), 2, "--"),
        ("--", Counter(ref=40), 2, "--"),
        ("II", Counter(), 2, "--"),
    ],
)
def test_evidence_and_ploidy_cannot_restore_rejected_or_absent_calls(
    gt, evidence, ploidy, expected
):
    assert supported_call(gt, SITE.site, evidence, ploidy) == expected


def test_par_boundaries_masked_y_and_mt_are_not_diploid_or_reference_filled(tmp_path):
    ploidy = tmp_path / "ploidy"
    ploidy.write_text(
        "chrX\t1\t80\tM\t2\nchrX\t81\t1000\tM\t1\n"
        "chrX\t1\t1000\tF\t2\nchrY\t1\t80\tM\t0\n"
        "chrY\t81\t1000\tM\t1\nchrY\t1\t1000\tF\t0\n"
        "chrM\t1\t1000\tM\t1\nchrM\t1\t1000\tF\t1\n"
    )
    for chrom, sex, expected in (
        ("chrX", "M", 1),
        ("chrX", "F", 2),
        ("chrY", "F", 0),
        ("chrY", "M", 1),
        ("chrM", "M", 1),
        ("chrM", "F", 1),
    ):
        probe = FlankedSite(IndelSite(chrom, 100, "CGT", "C", "I"), LEFT, RIGHT)
        assert expected_ploidy(ploidy, sex, probe) == expected
    boundary = FlankedSite(IndelSite("chrX", 85, "CGT", "C", "I"), LEFT, RIGHT)
    assert expected_ploidy(ploidy, "M", boundary) == 0


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
@pytest.mark.parametrize(
    ("kind", "policy", "expected"),
    [
        ("ref", {}, "II"),
        ("het", {}, "DI"),
        ("alt", {}, "DD"),
        ("het", {"min_dp": 100}, "--"),
        ("partial", {}, "--"),
        ("overlap", {}, "--"),
        ("none", {}, "--"),
        ("haploid_X", {}, "D"),
        ("haploid_Y", {}, "D"),
        ("haploid_MT", {}, "D"),
        ("female_Y", {}, "--"),
    ],
)
def test_real_alignment_native_mapping_normalization_and_vendor_only_overlay(
    tmp_path: Path,
    kind: str,
    policy: dict,
    expected: str,
):
    rng = random.Random(220)
    suffix = kind.split("_")[-1] if "_" in kind else "1"
    chrom = "chrM" if suffix == "MT" else "chr" + suffix
    sequence = "".join(rng.choices("ACGT", k=2000))
    sequence = sequence[:799] + "CGTA" + sequence[803:]
    fasta = write_fasta(tmp_path / "native.fa", {chrom: sequence})
    root = tmp_path / "templates"
    (root / "raw_file_templates/body").mkdir(parents=True)
    (root / "raw_file_templates/body/23andMe_V5_2025.txt").write_text(
        f"rsProbe\t{suffix}\t700\nrsAlias\t{suffix}\t700\n"
    )
    context = sequence[740:800] + "[GT/-]" + sequence[802:862]
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "Name,SNP,GenomeBuild,Chr,MapInfo,SourceSeq\n"
        f"rsProbe,[I/D],37,{suffix},700,{context}\n"
    )
    mapping = load_site_map(
        write_site_map(
            [SiteProjection(chrom, 700, chrom, 800, False, "mapped", "ref_equal")],
            {chrom: 2000},
            {chrom: 2000},
            tmp_path / "map",
            {},
        )
    )
    catalogue = CHM13Catalogue(
        mapping,
        {chrom: ReferenceContig(2000, hashlib.md5(sequence.encode()).hexdigest())},
        {chrom: 2000},
        (),
        "",
        "",
        "",
    )
    rows = [
        "@HD\tVN:1.6\tSO:coordinate",
        f"@SQ\tSN:{chrom}\tLN:2000",
        "@RG\tID:R\tSM:public-test",
    ]
    for i in range(20):
        alt = (
            kind == "alt" or kind.startswith("haploid_") or kind == "het" and i % 2 == 0
        )
        cigar = "60M2D60M" if alt else "122M"
        bases = sequence[740:800] + sequence[802:862] if alt else sequence[740:862]
        if kind == "partial":
            cigar, bases = "61M", sequence[740:801]
        if kind == "overlap" and i == 0:
            cigar, bases = "40M40D42M", sequence[740:780] + sequence[820:862]
        if kind == "none":
            continue
        read = _read(f"read{i}", cigar, bases, start=741)
        read[2] = chrom
        rows.append("\t".join(read) + "\tRG:Z:R")
    bam = sam_to_bam(rows, tmp_path / "sample.bam")
    ploidy = tmp_path / "ploidy"
    male = 1 if kind.startswith("haploid_") else 2
    female = 0 if suffix == "Y" else 1 if suffix == "MT" else 2
    ploidy.write_text(f"{chrom}\t1\t2000\tM\t{male}\n{chrom}\t1\t2000\tF\t{female}\n")
    sample_sex = tmp_path / "sex"
    sample_sex.write_text("public-test\tM\n")
    sex_args = (
        ["--samples-file", str(sample_sex)] if kind.startswith("haploid_") else []
    )
    out = tmp_path / "indels"
    result = call_chm13_indels(
        {"23andMe_V5_2025": str(manifest)},
        [str(root)],
        args=argparse.Namespace(input=bam, **policy),
        catalogue=catalogue,
        names={chrom: chrom},
        fasta=str(fasta),
        directory=out,
        regions=[],
        ploidy_path=ploidy,
        sex_args=sex_args,
        threads="1",
    )
    assert result == {"23andMe_V5_2025": {("rsProbe", suffix, "700"): expected}}
    summary = json.loads((out / "summary.json").read_text())
    assert summary["counts"]["verified_normalized_callable_probes"] == (
        0 if kind == "female_Y" else 1
    )
    assert summary["reference_filling"] is False


@needs_tools("bcftools", "samtools")
@pytest.mark.parametrize("inverted", [False, True])
def test_native_probe_identity_full_reference_flanks_and_static_overlap_quarantine(
    tmp_path, inverted
):
    rng = random.Random(220)
    sequence = "".join(rng.choices("ACGT", k=2000))
    sequence = sequence[:799] + "CGTA" + sequence[803:]
    fasta = write_fasta(tmp_path / "native.fa", {"chr1": sequence})
    mapping = load_site_map(
        write_site_map(
            [
                SiteProjection(
                    "chr1", pos, "chr1", pos + 100, inverted, "mapped", "ref_equal"
                )
                for pos in range(700, 703)
            ],
            {"chr1": 2000},
            {"chr1": 2000},
            tmp_path / "map",
            {},
        )
    )
    catalogue = CHM13Catalogue(
        mapping,
        {"chr1": ReferenceContig(2000, hashlib.md5(sequence.encode()).hexdigest())},
        {"chr1": 2000},
        (),
        "",
        "",
        "",
    )

    def probe(marker, pos, anchor, size):
        context = (
            sequence[anchor - 60 : anchor]
            + "["
            + sequence[anchor : anchor + size]
            + "/-]"
            + sequence[anchor + size : anchor + size + 60]
        )
        if inverted:
            context = context.translate(str.maketrans("ACGT[]", "TGCA]["))[::-1]
        return IndelProbe(marker, "1", pos, context)

    first = probe("first", 700, 800, 2)
    good = tmp_path / "good"
    good.mkdir()
    assert native_probe_sites(
        {"P0": first}, catalogue, {"chr1": "chr1"}, str(fasta), good
    ) == {
        "P0": IndelSite("chr1", 800, "CGT", "C", "I"),
    }
    bad = tmp_path / "bad"
    bad.mkdir()
    # P0/P1 share an anchor and are removed by normalization; P2 must remain
    # quarantined because its deletion overlaps those original definitions.
    assert (
        native_probe_sites(
            {
                "P0": first,
                "P1": probe("second", 701, 800, 3),
                "P2": probe("third", 702, 801, 1),
            },
            catalogue,
            {"chr1": "chr1"},
            str(fasta),
            bad,
        )
        == {}
    )
