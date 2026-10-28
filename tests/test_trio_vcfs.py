"""`vcf trio` from the proband's, mother's and father's VCFs, and `vcf filter`."""

import logging
import subprocess
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, run_wgsextract
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = needs_htslib

HEADER = (
    "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=100000>\n"
    '##INFO=<ID=END,Number=1,Type=Integer,Description="End">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype quality">\n'
)


def _vcf(
    path: Path,
    rows: list[str],
    sample: str = "S",
    header: str = "",
    chrom: str = "chr1",
) -> str:
    """A bgzipped, indexed single-sample VCF on chr1; rows are
    'POS ID REF ALT GT:GQ', tab-separated, with INFO given as '.'."""
    plain = path.with_suffix("")
    body = []
    for row in rows:
        pos, vid, ref, alt, fmt = row.split()
        info = "."
        if "|END=" in fmt:  # a gVCF reference block
            fmt, info = fmt.split("|")
        body.append(
            f"{chrom}\t{pos}\t{vid}\t{ref}\t{alt}\t50\tPASS\t{info}\tGT:GQ\t{fmt}"
        )
    plain.write_text(
        HEADER.replace("ID=chr1,", f"ID={chrom},")
        + header
        + f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample}\n"
        + "".join(line + "\n" for line in body)
    )
    return bgzip_index(plain, path)


def _ids(vcf: str | Path, region: str | None = None) -> list[str]:
    """IDs of the records, of those in region (read through the index)."""
    query = [get_tool_path("bcftools"), "query", "-f", "%ID\n"]
    out = subprocess.run(
        query + (["-r", region] if region else []) + [str(vcf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


def _trio(tmp_path: Path, child: list[str], mother: list[str], father: list[str]):
    return (
        _vcf(tmp_path / "child.vcf.gz", child),
        _vcf(tmp_path / "mother.vcf.gz", mother),
        _vcf(tmp_path / "father.vcf.gz", father),
    )


def _run_trio(files, outdir: Path, *extra: str) -> None:
    child, mother, father = files
    run_wgsextract(
        "vcf", "trio", "--proband", child, "--mother", mother, "--father", father,
        "--outdir", str(outdir), *extra,
    )  # fmt: skip


def test_trio_modes_report_their_inheritance_patterns(tmp_path):
    files = _trio(
        tmp_path,
        child=[
            "100 denovo A G 0/1:99",
            "200 inherited A G 0/1:99",
            "300 recessive A G 1/1:99",
            "400 comphet_mother A G 0/1:99",
            "500 comphet_father A G 0/1:99",
        ],
        mother=[
            "100 . A . 0/0:99",
            "200 . A G 1/1:99",
            "300 . A G 0/1:99",
            "400 . A G 0/1:99",
            "500 . A . 0/0:99",
        ],
        father=[
            "100 . A . 0/0:99",
            "200 . A . 0/0:99",
            "300 . A G 0/1:99",
            "400 . A . 0/0:99",
            "500 . A G 0/1:99",
        ],
    )
    _run_trio(files, tmp_path / "out", "--mode", "all")

    out = tmp_path / "out"
    assert _ids(out / "trio_denovo.vcf.gz") == ["denovo"]
    assert _ids(out / "trio_recessive.vcf.gz") == ["recessive"]
    assert _ids(out / "trio_comphet.vcf.gz") == ["comphet_mother", "comphet_father"]
    # The VCF path neither clusters nor leaves its merge behind.
    assert sorted(p.name for p in out.iterdir()) == [
        f"trio_{m}.vcf.gz{s}"
        for m in ("comphet", "denovo", "recessive")
        for s in ("", ".tbi")
    ]


def test_a_hom_ref_child_is_not_recessive(tmp_path):
    files = _trio(
        tmp_path,
        child=["100 hom_alt A G 1/1:99", "200 hom_ref A G 0/0:99"],
        mother=["100 . A G 0/1:99", "200 . A G 0/1:99"],
        father=["100 . A G 0/1:99", "200 . A G 0/1:99"],
    )
    _run_trio(files, tmp_path / "out", "--mode", "recessive")
    assert _ids(tmp_path / "out" / "trio_recessive.vcf.gz") == ["hom_alt"]


@pytest.mark.parametrize("input_option", ["--vcf-input", "--input"])
def test_trio_defaults_to_denovo_beside_the_proband_input(tmp_path, input_option):
    child, mother, father = _trio(
        tmp_path,
        child=["100 denovo A G 0/1:99"],
        mother=["100 . A . 0/0:99"],
        father=["100 . A . 0/0:99"],
    )
    run_wgsextract(
        "vcf", "trio", input_option, child, "--mother", mother, "--father", father
    )
    assert _ids(tmp_path / "trio_denovo.vcf.gz") == ["denovo"]
    assert not (tmp_path / "trio_recessive.vcf.gz").exists()


@pytest.mark.parametrize("selected", ["input", "vcf-input", "proband"])
def test_trio_proband_precedence_includes_explicit_input_and_configured_default(
    tmp_path, monkeypatch, selected
):
    child, mother, father = _trio(
        tmp_path,
        child=["100 chosen A G 0/1:99"],
        mother=["100 . A . 0/0:99"],
        father=["100 . A . 0/0:99"],
    )
    default = _vcf(tmp_path / "default.vcf.gz", ["100 wrong A G 0/1:99"])
    monkeypatch.setitem(settings, "default_input_vcf", default)
    options = ["--input", child if selected == "input" else default]
    if selected == "vcf-input":
        options += ["--vcf-input", child]
    elif selected == "proband":
        options += ["--vcf-input", default, "--proband", child]
    run_wgsextract(
        "vcf", "trio", *options, "--mother", mother, "--father", father,
        "--outdir", str(tmp_path / "out"),
    )  # fmt: skip
    assert _ids(tmp_path / "out" / "trio_denovo.vcf.gz") == ["chosen"]


def test_trio_region_limits_the_merge(tmp_path):
    files = _trio(
        tmp_path,
        child=["100 left A G 0/1:99", "5000 right A G 0/1:99"],
        mother=["100 . A . 0/0:99", "5000 . A . 0/0:99"],
        father=["100 . A . 0/0:99", "5000 . A . 0/0:99"],
    )
    _run_trio(files, tmp_path / "out", "--region", "chr1:4000-6000")
    assert _ids(tmp_path / "out" / "trio_denovo.vcf.gz") == ["right"]


def test_rerunning_trio_replaces_the_old_output_and_index(tmp_path):
    first = _trio(
        tmp_path,
        child=["100 first A G 0/1:99"],
        mother=["100 . A . 0/0:99"],
        father=["100 . A . 0/0:99"],
    )
    _run_trio(first, tmp_path / "out")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _trio(
        second_dir,
        child=["90000 second A G 0/1:99"],
        mother=["90000 . A . 0/0:99"],
        father=["90000 . A . 0/0:99"],
    )
    _run_trio(second, tmp_path / "out")
    # A stale index would still point into the first run's file.
    denovo = tmp_path / "out" / "trio_denovo.vcf.gz"
    assert _ids(denovo, "chr1:89000-91000") == ["second"]


def test_trio_needs_all_three_and_samples(tmp_path):
    child = _vcf(tmp_path / "child.vcf.gz", ["100 x A G 0/1:99"])
    with pytest.raises(WGSExtractError, match="--proband.*--vcf-input.*--input"):
        run_wgsextract("vcf", "trio", "--mother", child, "--father", child)
    with pytest.raises(WGSExtractError, match="--mother, and --father"):
        run_wgsextract("vcf", "trio", "--proband", child, "--mother", child)

    sites_only = tmp_path / "sites.vcf"
    sites_only.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=100000>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t100\t.\tA\tG\t50\t.\t.\n"
    )
    with pytest.raises(WGSExtractError, match="no samples"):
        run_wgsextract(
            "vcf", "trio", "--proband", child, "--mother", str(sites_only),
            "--father", child, "--outdir", str(tmp_path / "out"),
        )  # fmt: skip
    # The plain VCF was bgzipped and indexed beside itself to be read.
    assert Path(f"{sites_only}.gz.tbi").exists()


def test_trio_reports_high_impact_counts_when_annotated(tmp_path, caplog):
    csq = (
        '##INFO=<ID=CSQ,Number=.,Type=String,Description="Consequence '
        'annotations from Ensembl VEP. Format: Allele|IMPACT">\n'
    )
    child = tmp_path / "child.vcf"
    child.write_text(
        HEADER
        + csq
        + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "chr1\t100\thigh\tA\tG\t50\tPASS\tCSQ=G|HIGH\tGT:GQ\t0/1:99\n"
        + "chr1\t200\tlow\tA\tG\t50\tPASS\tCSQ=G|LOW\tGT:GQ\t0/1:99\n"
    )
    parent = ["100 . A . 0/0:99", "200 . A . 0/0:99"]
    files = (
        bgzip_index(child),
        _vcf(tmp_path / "mother.vcf.gz", parent),
        _vcf(tmp_path / "father.vcf.gz", parent),
    )
    with caplog.at_level(logging.INFO):
        _run_trio(files, tmp_path / "out")
    assert "DENOVO results: 2 total variants, 1 HIGH impact" in caplog.text


def _many(start: int, gt: str) -> list[str]:
    return [f"{start + 10 * i} v{i} A G {gt}" for i in range(120)]


@pytest.mark.parametrize(
    ("mother_gq", "count", "warned"),
    [
        (5, 120, True),  # GQ-0-like reference calls: inherited variants slip in
        (99, 120, False),  # confident reference calls
        (5, 99, False),  # too few calls to judge
    ],
)
def test_denovo_warns_when_parental_reference_calls_are_weak(
    tmp_path, caplog, mother_gq, count, warned
):
    child = [f"{100 + 10 * i} v{i} A G 0/1:99" for i in range(count)]
    mother = [f"{100 + 10 * i} . A . 0/0:{mother_gq}" for i in range(count)]
    father = [f"{100 + 10 * i} . A . 0/0:99" for i in range(count)]
    with caplog.at_level(logging.WARNING):
        _run_trio(_trio(tmp_path, child, mother, father), tmp_path / "out")
    assert len(_ids(tmp_path / "out" / "trio_denovo.vcf.gz")) == count
    assert ("rest on a parent's reference call with GQ under 20" in caplog.text) == (
        warned
    )
    if warned:
        assert f"{count:,} of these {count:,} de novo calls" in caplog.text


def test_parental_reference_block_starting_at_the_site_counts_as_reference(
    tmp_path,
):
    # A gVCF block that begins at the child's variant merges as the parent's 0/0.
    files = _trio(
        tmp_path,
        child=["1000 denovo A G 0/1:99"],
        mother=["1000 . A <NON_REF> 0/0:0|END=2000"],
        father=["1000 . A <NON_REF> 0/0:99|END=2000"],
    )
    _run_trio(files, tmp_path / "out")
    assert _ids(tmp_path / "out" / "trio_denovo.vcf.gz") == ["denovo"]


def test_parent_without_a_record_counts_as_missing(tmp_path):
    files = _trio(
        tmp_path,
        child=["100 denovo A G 0/1:99", "200 comphet A G 0/1:99"],
        # Variants-only VCFs: no hom-ref records.
        mother=["200 . A G 0/1:99"],
        father=["900 . C T 0/1:99"],
    )
    _run_trio(files, tmp_path / "out", "--mode", "all")
    assert _ids(tmp_path / "out" / "trio_denovo.vcf.gz") == ["denovo"]
    assert _ids(tmp_path / "out" / "trio_comphet.vcf.gz") == ["comphet"]


def test_a_parents_other_allele_does_not_hide_a_denovo(tmp_path):
    files = _trio(
        tmp_path,
        child=["100 denovo A T 0/1:99"],
        mother=["100 . A G 0/1:99"],
        father=["100 . A . 0/0:99"],
    )
    _run_trio(files, tmp_path / "out", "--mode", "all")
    assert _ids(tmp_path / "out" / "trio_comphet.vcf.gz") == []
    assert _ids(tmp_path / "out" / "trio_denovo.vcf.gz") == ["denovo"]


# --- vcf filter -----------------------------------------------------------------


def test_filter_keeps_the_region_and_expression_outside_gaps(tmp_path):
    vcf = _vcf(
        tmp_path / "s.vcf.gz",
        [
            "100 low_qual_gt A G 0/1:5",
            "150 in_gap A G 0/1:99",
            "300 kept A G 0/1:99",
            "90000 outside A G 0/1:99",
        ],
    )
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\n" + "A" * 100000 + "\n")
    (tmp_path / "ref_nbin.csv").write_text(
        "#SN\tBinID\tStart\tSize\nchr1\t1\t139\t20\nmalformed line\n"
        "chr1\t2\tnot-a-number\t5\n"
    )
    run_wgsextract(
        "vcf", "filter", "--vcf-input", vcf, "--ref", str(ref), "--region",
        "chr1:1-1000", "--expr", "GQ>=20", "--exclude-near-gaps",
        "--outdir", str(tmp_path / "out"),
    )  # fmt: skip
    assert _ids(tmp_path / "out" / "filtered.vcf.gz") == ["kept"]


def test_filter_without_gap_sidecar_warns_and_keeps_everything(tmp_path, caplog):
    vcf = _vcf(tmp_path / "s.vcf.gz", ["150 a A G 0/1:99"])
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\n" + "A" * 1000 + "\n")
    with caplog.at_level(logging.WARNING):
        run_wgsextract(
            "vcf", "filter", "--vcf-input", vcf, "--ref", str(ref),
            "--exclude-near-gaps", "--outdir", str(tmp_path / "out"),
        )  # fmt: skip
    assert "_nbin.csv) not found" in caplog.text
    assert _ids(tmp_path / "out" / "filtered.vcf.gz") == ["a"]


def test_rerunning_filter_replaces_the_index(tmp_path):
    vcf = _vcf(tmp_path / "s.vcf.gz", ["100 left A G 0/1:99", "90000 right A G 0/1:99"])
    out = tmp_path / "out"
    for region in ("chr1:1-1000", "chr1:89000-91000"):
        run_wgsextract(
            "vcf", "filter", "--vcf-input", vcf, "--region", region,
            "--outdir", str(out),
        )  # fmt: skip
    assert _ids(out / "filtered.vcf.gz", "chr1:89000-91000") == ["right"]


def test_gvcf_parents_reference_blocks_are_filled_in_at_the_childs_site(tmp_path):
    """With a reference, gVCFs merge with --gvcf: the mother's block (100-1000)
    gives her 0/0 at the child's site 500 rather than a missing genotype."""
    from tests.htslib_helpers import write_fasta

    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "T" * 1200})
    block = '##ALT=<ID=*,Description="Any other allele">\n'
    child = _vcf(tmp_path / "child.vcf.gz", ["500 dn T A 0/1:50"])
    mother = _vcf(
        tmp_path / "mother.vcf.gz", ["100 . T <*> 0/0:40|END=1000"], header=block
    )
    father = _vcf(tmp_path / "father.vcf.gz", ["900 inh T A 0/1:50"])
    out = tmp_path / "out"
    _run_trio((child, mother, father), out, "--ref", str(ref))

    assert _ids(out / "trio_denovo.vcf.gz") == ["dn"]
    genotypes = subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", "[%GT ]\n",
         str(out / "trio_denovo.vcf.gz")],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    assert genotypes[:2] == ["0/1", "0/0"]


@pytest.mark.parametrize(
    ("contig", "proband_prefix", "gvcf"),
    [
        ("11_KI270721v1_random", "chr", True),
        ("Un_KI270302v1", "chr", True),
        ("6_GL000250v2_alt", "", True),
        ("11_KI270721v1_random", "chr", False),
    ],
)
def test_trio_normalizes_parent_contigs_without_proband_records(
    tmp_path, contig, proband_prefix, gvcf
):
    from tests.htslib_helpers import write_fasta

    ref = write_fasta(
        tmp_path / "ref.fa", {"chr1": "T" * 1200, f"chr{contig}": "T" * 1200}
    )
    child = _vcf(
        tmp_path / "child.vcf.gz",
        ["500 dn T A 0/1:50"],
        header=f"##contig=<ID={proband_prefix}{contig},length=1200>\n",
        chrom=f"{proband_prefix}1",
    )
    parents = []
    for role in ("mother", "father"):
        plain = tmp_path / f"{role}.vcf"
        alt, info = ("<*>", "END=1000") if gvcf else (".", ".")
        plain.write_text(
            HEADER.replace("ID=chr1,", "ID=1,")
            + f"##contig=<ID={contig},length=1200>\n"
            + ('##ALT=<ID=*,Description="Any other allele">\n' if gvcf else "")
            + f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{role}\n"
            + f"1\t500\t.\tT\t{alt}\t50\tPASS\t{info}\tGT:GQ\t0/0:40\n"
            + f"{contig}\t100\t.\tT\t{alt}\t50\tPASS\t{info}\tGT:GQ\t0/0:40\n"
        )
        parents.append(bgzip_index(plain, Path(str(plain) + ".gz")))
    out = tmp_path / "out"
    _run_trio((child, *parents), out, *(["--ref", str(ref)] if gvcf else []))
    output = out / "trio_denovo.vcf.gz"
    assert _ids(output) == ["dn"]
    query = subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", "%CHROM[ %GT]\n", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert query == "chr1 0/1 0/0 0/0"
    header = subprocess.run(
        [get_tool_path("bcftools"), "view", "-h", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert f"##contig=<ID=chr{contig}," in header
    assert f"##contig=<ID={contig}," not in header
