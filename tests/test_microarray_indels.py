import argparse
import csv
import gzip
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.htslib_helpers import bgzip_index, needs_tools, sam_to_bam, write_fasta
from tests.test_vcf_basic import sample
from wgsextract_cli.core.microarray_indel_probes import (
    IndelProbe,
    IndelSite,
    match_indel_reference,
    read_indel_manifest,
)
from wgsextract_cli.core.microarray_indels import (
    _calls_from_vcf,
    _normalize_sites,
    call_manifest_indels,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command

LEFT = "AGCTTGAGTACGATCGCTAGTCAGTACGTC"
RIGHT = "CTGATCGTACGTAGCTAGTCGATCGTAGCT"
CONTEXT = LEFT + "[GT/-]" + RIGHT


def _manifest(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["[Header]"])
        writer.writerow(["Name", "SNP", "GenomeBuild", "Chr", "MapInfo", "SourceSeq"])
        writer.writerows(rows)
        writer.writerow(["[Controls]"])
    return str(path)


@pytest.mark.parametrize("suffix", [".csv", ".csv.gz", ".zip"])
def test_read_real_manifest_schema_and_supported_compression(tmp_path, suffix):
    path = tmp_path / "manifest.csv"
    _manifest(path, [["rsProbe", "[I/D]", "37", "1", "700", CONTEXT]])
    selected = tmp_path / ("input" + suffix)
    if suffix == ".zip":
        with zipfile.ZipFile(selected, "w") as archive:
            archive.write(path, "manifest.csv")
    elif suffix == ".csv.gz":
        with gzip.open(selected, "wb") as stream:
            stream.write(path.read_bytes())
    else:
        selected.write_bytes(path.read_bytes())
    assert read_indel_manifest(str(selected)) == {
        ("rsProbe", "1", 700): IndelProbe("rsProbe", "1", 700, CONTEXT)
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_probe_revisions_are_quarantined_regardless_of_order(
    tmp_path, reverse
):
    rows = [
        ["rsProbe", "[I/D]", "37", "1", "700", CONTEXT],
        ["rsProbe", "[I/D]", "37", "1", "700", LEFT + "[A/-]" + RIGHT],
        ["rsOther", "[D/I]", "37", "1", "800", CONTEXT],
    ]
    path = _manifest(tmp_path / "manifest.csv", rows[::-1] if reverse else rows)
    assert set(read_indel_manifest(path)) == {("rsOther", "1", 800)}


def test_unknown_reference_build_and_sequence_free_probes_are_rejected(tmp_path):
    path = _manifest(
        tmp_path / "manifest.csv",
        [["rsProbe", "[I/D]", "38", "1", "700", CONTEXT]],
    )
    with pytest.raises(WGSExtractError, match="GRCh37"):
        read_indel_manifest(path)
    _manifest(
        Path(path), [["rsProbe", "[I/D]", "37", "1", "700", LEFT + "[I/D]" + RIGHT]]
    )
    with pytest.raises(WGSExtractError, match="sequence-defined"):
        read_indel_manifest(path)


@pytest.mark.parametrize(
    "reference_allele,expected_ref,expected_alt,code",
    [
        ("GT", "CGT", "C", "I"),
        ("", "C", "CGT", "D"),
    ],
)
def test_reference_sequence_defines_insertion_versus_deletion(
    reference_allele, expected_ref, expected_alt, code
):
    left = LEFT[:-1] + "C"
    probe = IndelProbe("rsProbe", "1", 700, left + "[GT/-]" + RIGHT)
    site = match_indel_reference(probe, "chr1", 671, left + reference_allele + RIGHT)
    assert site is not None
    assert (site.position, site.ref, site.alt, site.ref_code) == (
        700,
        expected_ref,
        expected_alt,
        code,
    )


def test_complete_reverse_complement_flanks_are_required_not_strand_guessing():
    probe = IndelProbe("rsProbe", "1", 700, CONTEXT)
    sequence = (LEFT + "GT" + RIGHT).translate(str.maketrans("ACGT", "TGCA"))[::-1]
    site = match_indel_reference(probe, "chr1", 100, sequence)
    assert site is not None and site.ref_code == "I"
    assert match_indel_reference(probe, "chr1", 100, "A" * len(sequence)) is None
    assert match_indel_reference(probe, "chr1", 100, sequence + sequence) is None


@pytest.fixture(scope="module")
def alignment(tmp_path_factory):
    return sample.__wrapped__(tmp_path_factory)


@pytest.fixture
def probe_inputs(tmp_path, alignment):
    ref = Path(alignment["ref"])
    sequence = "".join(ref.read_text().splitlines()[1:])
    context = sequence[640:700] + "[GT/-]" + sequence[702:762]
    manifest = _manifest(
        tmp_path / "manifest.csv",
        [["rsProbe", "[I/D]", "37", "1", "700", context]],
    )
    root = tmp_path / "raw_file_templates"
    (root / "body").mkdir(parents=True)
    (root / "body/23andMe_V5_2025.txt").write_text("rsProbe\t1\t700\n")
    return manifest, SimpleNamespace(
        fasta=str(ref),
        build="hg19",
        root=None,
        ref_vcf_tab=None,
        ploidy_file=None,
        liftover_chain=None,
    )


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
@pytest.mark.parametrize(
    "kind,policy,expected",
    [
        ("homref", {}, "II"),
        ("het", {}, "DI"),
        ("homalt", {}, "DD"),
        ("het", {"min_dp": 30}, "--"),
    ],
)
def test_force_genotype_known_indel_alleles_with_real_native_caller(
    tmp_path, alignment, probe_inputs, kind, policy, expected
):
    manifest, lib = probe_inputs
    source = run_command(
        ["samtools", "view", "-h", alignment["bam"]], capture_output=True
    ).stdout.splitlines()
    lines = [
        line
        for line in source
        if line.startswith("@")
        or kind == "het"
        or ("D" in line.split("\t")[5]) == (kind == "homalt")
    ]
    bam = sam_to_bam(lines, tmp_path / "sample.bam")
    args = argparse.Namespace(input=bam, region="chr1:650-780", **policy)
    calls = call_manifest_indels(
        {"23andMe_V5_2025": manifest},
        [str(tmp_path)],
        args=args,
        lib=lib,
        outdir=str(tmp_path),
        ploidy_args=["--ploidy", "2"],
        threads="1",
    )
    assert calls == {"23andMe_V5_2025": {("rsProbe", "1", "700"): expected}}
    assert list(tmp_path.glob("Indels_*/normalized-probe-sites.vcf"))


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
@pytest.mark.parametrize(
    ("chrom", "region", "genotype", "expected"),
    [
        ("1", "chr1:650-780", "0/1", "DI"),
        ("1", "chr1:1-100", "0/1", "--"),
        ("chr2", "chr1:650-780", "0/1", "--"),
        ("1", "chr1:650-780", "1", "DD"),
    ],
)
def test_vcf_aliases_and_region_limits_never_call_absent_contigs(
    tmp_path, probe_inputs, chrom, region, genotype, expected
):
    manifest, lib = probe_inputs
    source = tmp_path / "input.vcf"
    source.write_text(
        f"##fileformat=VCFv4.2\n##contig=<ID={chrom},length=1000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
        f"{chrom}\t700\t.\tCGT\tC\t.\tPASS\t.\tGT\t{genotype}\n"
    )
    compressed = bgzip_index(source)
    calls = call_manifest_indels(
        {"23andMe_V5_2025": manifest},
        [str(tmp_path)],
        args=argparse.Namespace(input=str(compressed), region=region),
        lib=lib,
        outdir=str(tmp_path),
        ploidy_args=["--ploidy", "2"],
        threads="1",
    )
    assert calls == {"23andMe_V5_2025": {("rsProbe", "1", "700"): expected}}


@needs_tools("bcftools", "samtools")
def test_left_alignment_collapses_equivalent_probes_but_quarantines_distinct_events(
    tmp_path,
):
    fasta = write_fasta(tmp_path / "ref.fa", {"chr1": "CGTC" + "A" * 20 + "TGCA"})
    sites = {
        "P0": IndelSite("chr1", 15, "AA", "A", "I"),
        "P1": IndelSite("chr1", 16, "AA", "A", "I"),
    }
    normalized = _normalize_sites(sites, str(fasta), str(tmp_path))
    assert set(normalized.values()) == {IndelSite("chr1", 4, "CA", "C", "I")}
    sites["P2"] = IndelSite("chr1", 15, "AAA", "A", "I")
    assert _normalize_sites(sites, str(fasta), str(tmp_path)) == {}


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
def test_sequence_free_known_indel_ids_are_explicitly_missing(
    tmp_path, alignment, probe_inputs
):
    manifest, lib = probe_inputs
    valid = read_indel_manifest(manifest)[("rsProbe", "1", 700)]
    _manifest(
        Path(manifest),
        [
            ["rsProbe", "[I/D]", "37", "1", "700", valid.context],
            ["rsUnknown", "[I/D]", "37", "1", "701", LEFT + "[I/D]" + RIGHT],
        ],
    )
    with (tmp_path / "raw_file_templates/body/23andMe_V5_2025.txt").open("a") as stream:
        stream.write("rsUnknown\t1\t701\n")
    calls = call_manifest_indels(
        {"23andMe_V5_2025": manifest},
        [str(tmp_path)],
        args=argparse.Namespace(input=alignment["bam"], region="chr1:650-780"),
        lib=lib,
        outdir=str(tmp_path),
        ploidy_args=["--ploidy", "2"],
        threads="1",
    )
    assert calls["23andMe_V5_2025"] == {
        ("rsProbe", "1", "700"): "DI",
        ("rsUnknown", "1", "701"): "--",
    }


@needs_tools("bcftools")
@pytest.mark.parametrize(
    ("records", "policy", "expected"),
    [
        (["CGT\tC\tPASS\t0/0:12:99"], {}, "II"),
        (["CGT\tC\tPASS\t0/1:12:99"], {}, "DI"),
        (["CGT\tC\tPASS\t1/1:12:99"], {}, "DD"),
        (["CGT\tC\tPASS\t0/1:2:99"], {"min_dp": 10}, "--"),
        (["CGT\tC\tPASS\t0/1:12:5"], {"min_gq": 20}, "--"),
        (["CGT\tC\tLowQual\t0/1:12:99"], {"exclude_filtered": True}, "--"),
        (["CGT\tC\tPASS\t0/1:0:99"], {}, "--"),
        (["CGT\t<DEL>\tPASS\t0/0:12:99"], {}, "--"),
        (["CGT\tC\tPASS\t./.:12:99", "CGT\tC\tPASS\t0/1:12:99"], {}, "--"),
        (["CGT\tC\tPASS\t0/0:12:99", "CGT\tC\tPASS\t1/1:12:99"], {}, "--"),
    ],
)
def test_vcf_quality_unsupported_alleles_and_conflicts_cannot_restore_calls(
    tmp_path, records, policy, expected
):
    source = tmp_path / "input.vcf"
    header = (
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=1000>\n"
        '##FILTER=<ID=LowQual,Description="Low quality">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
        '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Quality">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
    )
    rows = []
    for record in records:
        ref, alt, filtered, call = record.split("\t")
        rows.append(f"chr1\t700\t.\t{ref}\t{alt}\t.\t{filtered}\t.\tGT:DP:GQ\t{call}\n")
    source.write_text(header + "".join(rows))
    result = _calls_from_vcf(
        str(source),
        {"P0": IndelSite("chr1", 700, "CGT", "C", "I")},
        argparse.Namespace(**policy),
        str(tmp_path),
    )
    assert result == {"P0": expected}


@needs_tools("bcftools")
@pytest.mark.parametrize("retired_source", [False, True])
def test_identity_rejection_of_probe_or_source_cannot_be_restored(
    tmp_path, monkeypatch, retired_source
):
    from wgsextract_cli.core import microarray_identity as identity

    monkeypatch.setattr(
        identity,
        "load_rsid_map",
        lambda: {"rs20": identity.RsidIdentity(None, "withdrawn")},
    )
    source = tmp_path / "input.vcf"
    identifier = "rs20" if retired_source else "."
    source.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=1000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
        f"chr1\t700\t{identifier}\tCGT\tC\t.\tPASS\t.\tGT\t0/1\n"
        "chr1\t700\t.\tCGT\tC\t.\tPASS\t.\tGT\t0/1\n"
    )
    calls = _calls_from_vcf(
        str(source),
        {"P0": IndelSite("chr1", 700, "CGT", "C", "I")},
        argparse.Namespace(resolve_rsids=True),
        str(tmp_path),
        {"P0": "rs1" if retired_source else "rs20"},
    )
    assert calls == {"P0": "--"}


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
@pytest.mark.parametrize(("gt", "code"), [("0", "I"), ("1", "D")])
def test_haploid_indels_follow_each_verified_vendor_ploidy_style(
    tmp_path, alignment, gt, code
):
    sequence = "".join(Path(alignment["ref"]).read_text().splitlines()[1:])
    ref = write_fasta(tmp_path / "ref.fa", {"chrY": sequence})
    manifest = _manifest(
        tmp_path / "manifest.csv",
        [
            [
                "rsProbe",
                "[I/D]",
                "37",
                "Y",
                "700",
                sequence[640:700] + "[GT/-]" + sequence[702:762],
            ]
        ],
    )
    bodies = tmp_path / "raw_file_templates/body"
    bodies.mkdir(parents=True)
    (bodies / "23andMe_V5_2025.txt").write_text("rsProbe\tY\t700\n")
    (bodies / "Ancestry_V2_2024.txt").write_text("rsProbe\t24\t700\n")
    (bodies / "MyHeritage_GSA_2025.csv").write_text('"rsProbe","Y","700",\n')
    source = tmp_path / "input.vcf"
    source.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chrY,length=1000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
        f"chrY\t700\t.\tCGT\tC\t.\tPASS\t.\tGT\t{gt}\n"
    )
    calls = call_manifest_indels(
        dict.fromkeys(
            ("23andMe_V5_2025", "Ancestry_V2_2024", "MyHeritage_GSA_2025"), manifest
        ),
        [str(tmp_path)],
        args=argparse.Namespace(input=bgzip_index(source), region="chrY"),
        lib=SimpleNamespace(fasta=str(ref), build="hg19", root=None),
        outdir=str(tmp_path),
        ploidy_args=["--ploidy", "GRCh37"],
        threads="1",
    )
    assert calls == {
        "23andMe_V5_2025": {("rsProbe", "Y", "700"): code},
        "Ancestry_V2_2024": {("rsProbe", "24", "700"): code * 2},
        "MyHeritage_GSA_2025": {("rsProbe", "Y", "700"): code * 2},
    }
