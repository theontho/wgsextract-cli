"""VCF annotation commands (vcf clinvar/revel/phylop/gnomad/spliceai/alphamissense/
pharmgkb/gerp/chain-annotate) on tiny VCFs and annotation sources, and the
downloads that install those sources."""

import gzip
import io
import os
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

needs_bcftools = needs_tools("bcftools", "bgzip", "tabix")

# chr1's length identifies the build of a VCF to the reference library.
HG38_CHR1 = 248956422
HG19_CHR1 = 249250621

INFO_HEADERS = {
    "CLNSIG": '##INFO=<ID=CLNSIG,Number=.,Type=String,Description="s">',
    "CLNDN": '##INFO=<ID=CLNDN,Number=.,Type=String,Description="d">',
    "SpliceAI": '##INFO=<ID=SpliceAI,Number=.,Type=String,Description="s">',
    "AF": '##INFO=<ID=AF,Number=A,Type=Float,Description="f">',
    "PHARMGKB": '##INFO=<ID=PHARMGKB,Number=.,Type=String,Description="p">',
    "REVEL": '##INFO=<ID=REVEL,Number=1,Type=Float,Description="r">',
    "PHYLOP": '##INFO=<ID=PHYLOP,Number=1,Type=Float,Description="p">',
    "am_pathogenicity": '##INFO=<ID=am_pathogenicity,Number=1,Type=Float,Description="a">',
    "am_class": '##INFO=<ID=am_class,Number=1,Type=String,Description="c">',
}


def _bgzip(path: Path, text: str) -> str:
    """bgzip text to path (no index)."""
    with open(path, "wb") as out:
        subprocess.run(
            [get_tool_path("bgzip"), "-c"], input=text.encode(), stdout=out, check=True
        )
    return str(path)


def _vcf(
    path: Path,
    rows: list[str],
    contig: str = "chr1",
    length: int = HG38_CHR1,
    info: tuple[str, ...] = (),
) -> str:
    """A bgzipped, indexed one-sample VCF; rows are tab-joined from spaces."""
    text = (
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={contig},length={length}>\n"
        '##INFO=<ID=END,Number=1,Type=Integer,Description="e">\n'
        + "".join(INFO_HEADERS[tag] + "\n" for tag in info)
        + '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        + "".join(f"{contig}\t" + "\t".join(r.split()) + "\n" for r in rows)
    )
    _bgzip(path, text)
    subprocess.run([get_tool_path("tabix"), "-f", "-p", "vcf", str(path)], check=True)
    return str(path)


def _tsv(path: Path, header: str, rows: list[str]) -> str:
    """A bgzipped (not indexed) TSV; rows are tab-joined from spaces."""
    lines = [header, *("\t".join(r.split()) for r in rows)]
    return _bgzip(path, "\n".join(lines) + "\n")


def _query(vcf: str | Path, fmt: str = "%ID:%INFO\n") -> list[str]:
    res = subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", fmt, str(vcf)],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.split()


def _sample(tmp_path: Path, rows: list[str], **kwargs) -> str:
    sample_dir = tmp_path / "in"
    sample_dir.mkdir(exist_ok=True)
    return _vcf(sample_dir / "sample.vcf.gz", rows, **kwargs)


# A SNV, a multiallelic site, a gVCF reference block and an indel.
SAMPLE_ROWS = [
    "100 snv C T . PASS . GT 0/1",
    "200 diffalt A G . PASS . GT 0/1",
    "300 block G <NON_REF> . PASS END=310 GT 0/0",
    "400 del CA C . PASS . GT 1/1",
]


# --- SpliceAI / PharmGKB / AlphaMissense (vcf/splicing.py) ------------------------


@needs_bcftools
def test_spliceai_uses_the_library_file_of_the_vcfs_build(tmp_path):
    library = tmp_path / "lib" / "ref"
    library.mkdir(parents=True)
    # Ensembl's scores name contigs 1, 2, ...; the sample uses chr1.
    for build, length, score in (
        ("hg38", HG38_CHR1, "0.9"),
        ("hg19", HG19_CHR1, "0.1"),
    ):
        _vcf(
            library / f"spliceai_{build}.vcf.gz",
            [
                f"100 . C T . . SpliceAI=T|G1|{score}|0|0|0|1|2|3|4 GT 0/1",
                f"200 . A C . . SpliceAI=C|G1|{score}|0|0|0|1|2|3|4 GT 0/1",
                f"300 . G A . . SpliceAI=A|G1|{score}|0|0|0|1|2|3|4 GT 0/1",
            ],
            contig="1",
            length=length,
            info=("SpliceAI",),
        )
    outdir = tmp_path / "out"
    for length in (HG38_CHR1, HG19_CHR1):
        sample = _sample(tmp_path, SAMPLE_ROWS, length=length)
        run_wgsextract("vcf", "spliceai", "--input", sample, "--ref",
                       str(tmp_path / "lib"), "--outdir", str(outdir))  # fmt: skip
        score = "0.9" if length == HG38_CHR1 else "0.1"
        # Matched by allele: another ALT at 200 and the block at 300 get nothing.
        assert _query(
            outdir / "spliceai_annotated.vcf.gz", "%CHROM:%ID:%SpliceAI\n"
        ) == [
            f"chr1:snv:T|G1|{score}|0|0|0|1|2|3|4",
            "chr1:diffalt:.",
            "chr1:block:.",
            "chr1:del:.",
        ]
    assert not [n for n in os.listdir(outdir) if n.startswith(".wgse")]


@needs_bcftools
def test_spliceai_without_scores_names_the_ref_command(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    (tmp_path / "lib" / "ref").mkdir(parents=True)
    with pytest.raises(WGSExtractError, match="wgsextract ref spliceai"):
        run_wgsextract("vcf", "spliceai", "--input", sample,
                       "--ref", str(tmp_path / "lib"))  # fmt: skip
    assert sorted(os.listdir(tmp_path / "in")) == ["sample.vcf.gz", "sample.vcf.gz.tbi"]


AM_HEADER = (
    "# Copyright 2023 DeepMind Technologies Limited\n"
    "#CHROM\tPOS\tREF\tALT\tgenome\tuniprot_id\ttranscript_id\tprotein_variant\t"
    "am_pathogenicity\tam_class"
)


@needs_bcftools
def test_alphamissense_table_transfers_score_and_class_and_filters(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    table = _tsv(
        tmp_path / "alphamissense_hg38.tsv.gz",
        AM_HEADER,
        [
            "chr1 100 C T hg38 P1 ENST1 A1V 0.9512 likely_pathogenic",
            "chr1 200 A C hg38 P1 ENST1 K2E 0.99 likely_pathogenic",
            "chr1 300 G A hg38 P1 ENST1 G3R 0.98 likely_pathogenic",
            "chr1 400 C G hg38 P1 ENST1 L4V 0.2 likely_benign",
        ],
    )
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "alphamissense", "--input", sample, "--am-file", table,
                   "--outdir", str(outdir), "--min-score", "0.5")  # fmt: skip

    assert _query(
        outdir / "alphamissense_annotated.vcf.gz", "%ID:%am_pathogenicity:%am_class\n"
    ) == ["snv:0.9512:likely_pathogenic", "diffalt:.:.", "block:.:.", "del:.:."]
    assert _query(outdir / "alphamissense_gt_0.5.vcf.gz", "%ID\n") == ["snv"]
    assert Path(table + ".tbi").is_file()  # indexed beside the table


@needs_bcftools
def test_alphamissense_vcf_resource_keeps_its_own_header(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    am_vcf = _vcf(
        tmp_path / "am.vcf.gz",
        ["100 . C T . . am_pathogenicity=0.7;am_class=ambiguous GT 0/1"],
        contig="1",
        info=("am_pathogenicity", "am_class"),
    )
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "alphamissense", "--input", sample, "--am-file", am_vcf,
                   "--outdir", str(outdir))  # fmt: skip
    assert _query(
        outdir / "alphamissense_annotated.vcf.gz", "%ID:%am_pathogenicity:%am_class\n"
    ) == ["snv:0.7:ambiguous", "diffalt:.:.", "block:.:.", "del:.:."]
    assert not (outdir / "alphamissense_gt_None.vcf.gz").exists()


@needs_bcftools
def test_pharmgkb_vcf_resource_annotates_by_allele(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    pharmgkb = tmp_path / "pharmgkb.vcf"
    pharmgkb.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1>\n"
        + INFO_HEADERS["PHARMGKB"]
        + "\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\trs1\tC\tT\t.\t.\tPHARMGKB=rs1|CYP2C19|1A|Metabolism/PK|x\n"
        "chr1\t300\trs3\tG\tA\t.\t.\tPHARMGKB=rs3|VKORC1|1A|Dosage|warfarin\n"
    )
    outdir = tmp_path / "out"
    # A plain VCF is bgzipped and indexed beside itself first.
    run_wgsextract("vcf", "pharmgkb", "--input", sample, "--pharmgkb-file",
                   str(pharmgkb), "--outdir", str(outdir))  # fmt: skip
    assert _query(outdir / "pharmgkb_annotated.vcf.gz", "%ID:%PHARMGKB\n") == [
        "snv:rs1|CYP2C19|1A|Metabolism/PK|x",
        "diffalt:.",
        "block:.",
        "del:.",
    ]
    assert Path(f"{pharmgkb}.gz.tbi").is_file()


# --- ClinVar / REVEL (vcf/clinvar_revel.py) ---------------------------------------


@needs_bcftools
def test_clinvar_picks_the_builds_file_and_filters_pathogenic_classes(tmp_path):
    library = tmp_path / "lib" / "ref"
    library.mkdir(parents=True)
    sigs = ["Pathogenic", "Likely_pathogenic", "Pathogenic/Likely_pathogenic",
            "Conflicting_classifications_of_pathogenicity",
            "Uncertain_significance"]  # fmt: skip
    rows = [f"{100 * (i + 1)} v{i} A G . PASS . GT 0/1" for i in range(len(sigs))]
    for build, length in (("hg19", HG19_CHR1), ("hg38", HG38_CHR1)):
        # Only hg19's ClinVar has these classifications; hg38's calls all Benign.
        _vcf(
            library / f"clinvar_{build}.vcf.gz",
            [
                f"{100 * (i + 1)} c{i} A G . . "
                f"CLNSIG={sig if build == 'hg19' else 'Benign'};CLNDN=D{i} GT 0/1"
                for i, sig in enumerate(sigs)
            ],
            contig="1",
            length=length,
            info=("CLNSIG", "CLNDN"),
        )
    sample = _sample(tmp_path, rows, length=HG19_CHR1)
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "clinvar", "--input", sample, "--ref",
                   str(tmp_path / "lib"), "--outdir", str(outdir))  # fmt: skip
    assert _query(outdir / "clinvar_annotated.vcf.gz", "%ID:%CLNSIG:%CLNDN\n") == [
        f"v{i}:{sig}:D{i}" for i, sig in enumerate(sigs)
    ]
    assert _query(outdir / "clinvar_pathogenic.vcf.gz", "%ID\n") == ["v0", "v1", "v2"]


REVEL_HEADER = "#Chr\tStart\tEnd\tRef\tAlt\tREVEL"


@needs_bcftools
def test_revel_table_skips_end_column_and_filters_by_score(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    # Annovar's table: #Chr, Start, End, Ref, Alt, REVEL, on contigs 1, 2, ...
    table = _tsv(
        tmp_path / "revel_hg38.tsv.gz",
        REVEL_HEADER,
        ["1 100 100 C T 0.812", "1 200 200 A C 0.9", "1 300 300 G A 0.7"],
    )
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "revel", "--input", sample, "--revel-file", table,
                   "--outdir", str(outdir), "--min-score", "0.8")  # fmt: skip
    assert _query(outdir / "revel_annotated.vcf.gz", "%CHROM:%ID:%REVEL\n") == [
        "chr1:snv:0.812", "chr1:diffalt:.", "chr1:block:.", "chr1:del:."
    ]  # fmt: skip
    assert _query(outdir / "revel_gt_0.8.vcf.gz", "%ID\n") == ["snv"]


@needs_bcftools
def test_revel_vcf_resource(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    revel = _vcf(tmp_path / "revel.vcf.gz", ["100 . C T . . REVEL=0.4 GT 0/1"],
                 info=("REVEL",))  # fmt: skip
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "revel", "--input", sample, "--revel-file", revel,
                   "--outdir", str(outdir))  # fmt: skip
    assert _query(outdir / "revel_annotated.vcf.gz", "%ID:%REVEL\n") == [
        "snv:0.4", "diffalt:.", "block:.", "del:."
    ]  # fmt: skip


# --- PhyloP tables (vcf/population.py) -----------------------------------------


@needs_bcftools
def test_phylop_table_and_vcf_score_by_position(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    table = _tsv(
        tmp_path / "phylop.tsv.gz",
        "#Chr\tStart\tEnd\tScore",
        ["1 100 100 3.25", "1 200 200 -1.5", "1 400 400 0.5"],
    )
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "phylop", "--input", sample, "--phylop-file", table,
                   "--outdir", str(outdir), "--min-score", "2")  # fmt: skip
    # By position alone: conservation doesn't depend on the ALT.
    assert _query(outdir / "phylop_annotated.vcf.gz", "%ID:%PHYLOP\n") == [
        "snv:3.25", "diffalt:-1.5", "block:.", "del:0.5"
    ]  # fmt: skip
    assert _query(outdir / "phylop_gt_2.0.vcf.gz", "%ID\n") == ["snv"]

    phylop_vcf = _vcf(tmp_path / "phylop.vcf.gz", ["200 . A . . . PHYLOP=4 GT 0/0"],
                      info=("PHYLOP",))  # fmt: skip
    run_wgsextract("vcf", "phylop", "--input", sample, "--phylop-file", phylop_vcf,
                   "--outdir", str(tmp_path / "out2"))  # fmt: skip
    assert _query(tmp_path / "out2" / "phylop_annotated.vcf.gz", "%ID:%PHYLOP\n") == [
        "snv:.", "diffalt:.", "block:.", "del:."
    ]  # fmt: skip


# --- shared machinery (vcf/annotation_helpers.py) --------------------------------


@needs_bcftools
def test_plain_table_is_bgzipped_once_and_reindexed_when_newer(tmp_path):
    from wgsextract_cli.commands.vcf.annotation_helpers import prepare_tabix_annotation

    plain = tmp_path / "revel.tsv"
    plain.write_text(f"{REVEL_HEADER}\n1\t100\t100\tC\tT\t0.5\n")
    prepared = prepare_tabix_annotation(str(plain), "REVEL")
    assert prepared == f"{plain}.gz"
    assert gzip.open(prepared, "rt").read().endswith("0.5\n")
    first = Path(prepared).stat().st_mtime_ns

    # Unchanged: neither recompressed nor reindexed.
    assert prepare_tabix_annotation(str(plain), "REVEL") == prepared
    assert Path(prepared).stat().st_mtime_ns == first

    # The source edited later: compressed and indexed again.
    plain.write_text(f"{REVEL_HEADER}\n1\t100\t100\tC\tT\t0.75\n")
    os.utime(plain, ns=(first + 10**9, first + 10**9))
    prepare_tabix_annotation(str(plain), "REVEL")
    assert gzip.open(prepared, "rt").read().endswith("0.75\n")
    index = Path(f"{prepared}.tbi")
    assert index.stat().st_mtime_ns > Path(prepared).stat().st_mtime_ns - 1


def test_plain_gzip_table_is_rejected_with_a_recompress_hint(tmp_path):
    from wgsextract_cli.commands.vcf.annotation_helpers import prepare_tabix_annotation

    table = tmp_path / "revel_hg38.tsv.gz"
    table.write_bytes(gzip.compress(f"{REVEL_HEADER}\n".encode()))
    with pytest.raises(WGSExtractError, match="plain gzip, not BGZF.*bgzip"):
        prepare_tabix_annotation(str(table), "REVEL")
    assert os.listdir(tmp_path) == ["revel_hg38.tsv.gz"]


@needs_bcftools
def test_unsorted_table_fails_to_index_with_its_path(tmp_path):
    from wgsextract_cli.commands.vcf.annotation_helpers import prepare_tabix_annotation

    table = _tsv(tmp_path / "t.tsv.gz", REVEL_HEADER,
                 ["1 200 200 A G 0.1", "1 100 100 C T 0.2"])  # fmt: skip
    with pytest.raises(WGSExtractError, match=f"Indexing {table} failed"):
        prepare_tabix_annotation(table, "REVEL")


def test_contigs_of_an_unindexed_table_are_unknown(tmp_path):
    from wgsextract_cli.commands.vcf.annotation_helpers import (
        _contigs_by_size,
        annotation_chromosomes,
    )

    missing = str(tmp_path / "missing.tsv.gz")
    assert annotation_chromosomes(missing) == []
    assert _contigs_by_size(missing) == []


@needs_bcftools
def test_failed_annotation_reports_the_label_and_bcftools_error(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    table = _tsv(tmp_path / "revel.tsv.gz", REVEL_HEADER,
                 ["1 100 100 C T not_a_number"])  # fmt: skip
    with pytest.raises(
        WGSExtractError, match="(?s)REVEL annotation failed.*not_a_number"
    ):
        run_wgsextract("vcf", "revel", "--input", sample, "--revel-file", table,
                       "--outdir", str(tmp_path / "out"))  # fmt: skip


def test_failed_filter_is_reported_with_its_label(tmp_path):
    from wgsextract_cli.commands.vcf.annotation_helpers import run_filter

    with pytest.raises(WGSExtractError, match="REVEL filtering failed"):
        run_filter(str(tmp_path / "missing.vcf.gz"), str(tmp_path / "o.vcf.gz"),
                   "REVEL >= 1", "REVEL")  # fmt: skip


@needs_bcftools
def test_gerp_refuses_a_bigwig_without_the_vcfs_contigs(tmp_path):
    pyBigWig = pytest.importorskip("pyBigWig")
    bigwig = str(tmp_path / "gerp.bw")
    bw = pyBigWig.open(bigwig, "w")
    bw.addHeader([("chr2", 1000)])
    bw.addEntries("chr2", 0, values=[1.0], span=1, step=1)
    bw.close()
    sample = _sample(tmp_path, SAMPLE_ROWS)
    with pytest.raises(WGSExtractError, match="None of the VCF's contigs"):
        run_wgsextract("vcf", "gerp", "--input", sample, "--gerp-file", bigwig,
                       "--outdir", str(tmp_path / "out"))  # fmt: skip
    assert not [n for n in os.listdir(tmp_path / "out") if n.startswith(".wgse")]


@needs_bcftools
def test_gerp_scores_gvcf_and_multiallelic_variant_records(tmp_path):
    pyBigWig = pytest.importorskip("pyBigWig")
    bigwig = str(tmp_path / "gerp.bw")
    bw = pyBigWig.open(bigwig, "w")
    bw.addHeader([("1", 1000)])
    bw.addEntries("1", 99, values=[2.5, 3.5], span=1, step=1)
    bw.close()
    sample = _sample(tmp_path, [
        "100 gvcf C T,<NON_REF> . PASS . GT 0/1",
        "101 multi A G,T . PASS . GT 1/2",
        "101 star A *,G . PASS . GT 1/2",
    ], length=1000)  # fmt: skip
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "gerp", "--input", sample, "--gerp-file", bigwig,
                   "--outdir", str(outdir))  # fmt: skip
    # Split per ALT: the <NON_REF> and * halves get no score.
    assert _query(outdir / "gerp_annotated.vcf.gz", "%ID:%ALT:%GERP\n") == [
        "gvcf:T:2.5", "gvcf:<NON_REF>:.", "multi:G:3.5", "multi:T:3.5",
        "star:*:.", "star:G:3.5",
    ]  # fmt: skip


# --- chain-annotate, end to end ---------------------------------------------------


@needs_bcftools
def test_chain_annotate_runs_each_installed_step_on_the_last_output(tmp_path):
    library = tmp_path / "lib" / "ref"
    library.mkdir(parents=True)
    _vcf(library / "clinvar_hg38.vcf.gz",
         ["100 c1 C T . . CLNSIG=Pathogenic;CLNDN=X GT 0/1"],
         contig="1", info=("CLNSIG", "CLNDN"))  # fmt: skip
    table = _tsv(library / "revel_hg38.tsv.gz", REVEL_HEADER, ["1 100 100 C T 0.9"])
    subprocess.run([get_tool_path("tabix"), "-s1", "-b2", "-e2", table], check=True)
    sample = _sample(tmp_path, SAMPLE_ROWS)
    outdir = tmp_path / "out"
    # gerp isn't installed: skipped with a warning, not a failure.
    run_wgsextract("vcf", "chain-annotate", "--input", sample, "--ref",
                   str(tmp_path / "lib"), "--outdir", str(outdir),
                   "--annotations", "clinvar, gerp ,REVEL")  # fmt: skip
    final = outdir / "chain_annotated.vcf.gz"
    assert _query(final, "%CHROM:%ID:%CLNSIG:%REVEL\n") == [
        "chr1:snv:Pathogenic:0.9", "chr1:diffalt:.:.", "chr1:block:.:.", "chr1:del:.:."
    ]  # fmt: skip
    assert sorted(os.listdir(outdir)) == [
        "chain_annotated.vcf.gz",
        "chain_annotated.vcf.gz.tbi",
    ]


def test_chain_step_without_its_output_fails_and_keeps_intermediates(tmp_path):
    from wgsextract_cli.commands.vcf import chain

    input_vcf = tmp_path / "in.vcf.gz"
    input_vcf.write_bytes(b"")

    class Lib:
        clinvar_vcf = "present"

        def __init__(self, *_a, **_k):
            pass

    commands = []

    def silent_step(cmd, **_kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "wrote nothing\n")

    with (
        patch.object(chain, "verify_dependencies"),
        patch.object(chain, "get_tool_path", return_value="bcftools"),
        patch.object(chain, "ReferenceLibrary", Lib),
        patch.object(chain, "ensure_vcf_prepared", side_effect=lambda p: p),
        patch.object(chain, "run_command", side_effect=silent_step),
        pytest.raises(WGSExtractError, match="'clinvar' did not produce"),
    ):
        run_wgsextract("vcf", "chain-annotate", "--input", str(input_vcf),
                       "--ref", "/lib", "--annotations", "clinvar")  # fmt: skip
    assert commands[0][-2:] == ["--ref", "/lib"]
    assert sorted(os.listdir(tmp_path)) == ["chain_step_1_clinvar", "in.vcf.gz"]


@needs_bcftools
def test_cadd_without_indel_scores_annotates_snvs_only(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    snvs = _tsv(tmp_path / "cadd.tsv.gz", "#Chrom\tPos\tRef\tAlt\tRawScore\tPHRED",
                ["1 100 C T 2.5 24.1", "1 200 A C 3 30"])  # fmt: skip
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "cadd", "--input", sample, "--cadd-file", snvs,
                   "--outdir", str(outdir))  # fmt: skip
    assert _query(outdir / "cadd_annotated.vcf.gz", "%ID:%CADD_RAW:%CADD_PHRED\n") == [
        "snv:2.5:24.1", "diffalt:.:.", "block:.:.", "del:.:."
    ]  # fmt: skip


# --- installing the sources (core/annotation_resources.py, core/pharmgkb.py) -----


def test_genome_size_counts_the_genome_and_its_indexes(tmp_path):
    from wgsextract_cli.core.annotation_resources import get_genome_size

    genomes = tmp_path / "genomes"
    genomes.mkdir()
    (genomes / "hg38.fa.gz").write_bytes(b"x" * 1000)
    (genomes / "hg38.fa.gz.fai").write_bytes(b"x" * 24)
    (genomes / "hg38.fa.gz.gzi").write_bytes(b"x" * 1024)
    assert get_genome_size("hg38.fa.gz", str(tmp_path)) == "2.0 KiB"
    assert get_genome_size("hg19.fa.gz", str(tmp_path)) == ""
    assert get_genome_size("hg38.fa.gz", "") == ""


CLINVAR_SOURCE = (
    "##fileformat=VCFv4.2\n##contig=<ID=1>\n" + INFO_HEADERS["CLNSIG"] + "\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "1\t100\t1\tC\tT\t.\t.\tCLNSIG=Pathogenic\n"
)


@needs_bcftools
def test_clinvar_download_recompresses_and_indexes_each_build(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    def plain_gzip(url, dest, *_args):
        if url == ar.CLINVAR_URLS["hg19"]:
            return False  # this build's download fails
        Path(dest).write_bytes(gzip.compress(CLINVAR_SOURCE.encode()))
        return True

    with patch.object(ar, "download_file", side_effect=plain_gzip):
        assert ar.download_clinvar(str(tmp_path)) is False
    ref = tmp_path / "ref"
    assert sorted(os.listdir(ref)) == ["clinvar_hg38.vcf.gz", "clinvar_hg38.vcf.gz.tbi"]
    with open(ref / "clinvar_hg38.vcf.gz", "rb") as f:
        assert f.read(4) == b"\x1f\x8b\x08\x04"  # BGZF now
    assert _query(ref / "clinvar_hg38.vcf.gz", "%CHROM:%POS:%CLNSIG\n") == [
        "1:100:Pathogenic"
    ]


def test_resource_download_stops_when_cancelled(tmp_path):
    import threading

    from wgsextract_cli.core import annotation_resources as ar

    cancel = threading.Event()

    def cancel_after_first(_url, dest, *_args):
        Path(dest).write_bytes(gzip.compress(CLINVAR_SOURCE.encode()))
        cancel.set()
        return True

    with patch.object(ar, "download_file", side_effect=cancel_after_first) as dl:
        assert ar.download_clinvar(str(tmp_path), cancel_event=cancel) is False
    assert dl.call_count == 1  # hg38 only; never indexed, hg19 never started
    assert not (tmp_path / "ref" / "clinvar_hg38.vcf.gz.tbi").exists()


@needs_bcftools
def test_manual_spliceai_hg19_file_is_indexed_too(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "spliceai_hg19.vcf.gz").write_bytes(gzip.compress(CLINVAR_SOURCE.encode()))
    with (
        patch.dict(ar.SPLICEAI_URLS, {"hg38": "https://example.invalid/s.vcf.gz"}),
        patch.object(ar, "download_file", return_value=False) as dl,
    ):
        # hg38's download fails, but the hand-saved hg19 file is still indexed.
        assert ar.download_spliceai(str(tmp_path)) is False
    assert dl.call_count == 1
    assert (ref / "spliceai_hg19.vcf.gz.tbi").is_file()


@needs_bcftools
@pytest.mark.parametrize(
    ("problem", "message"),
    [("no_records", "no records on 2"), ("not_bgzf", "truncated")],
)
def test_published_index_check_rejects_bad_pairs(tmp_path, problem, message):
    from wgsextract_cli.core.annotation_resources import _check_indexed

    vcf = tmp_path / "s.vcf.gz"
    text = CLINVAR_SOURCE.replace("##contig=<ID=1>", "##contig=<ID=1>\n##contig=<ID=2>")
    _bgzip(vcf, text)
    subprocess.run([get_tool_path("tabix"), "-p", "vcf", str(vcf)], check=True)
    if problem == "no_records":
        # An index listing a contig whose records were cut from the data.
        two = text + "2\t5\t.\tA\tG\t.\t.\t.\n"
        _bgzip(vcf, two)
        subprocess.run(
            [get_tool_path("tabix"), "-f", "-p", "vcf", str(vcf)], check=True
        )
        _bgzip(vcf, text)
        message_re = "no records on 2|reading 2 failed"
    else:
        vcf.write_bytes(gzip.compress(text.encode()))
        message_re = message
    with pytest.raises(WGSExtractError, match=message_re):
        _check_indexed(str(vcf))


def _md5(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def test_cadd_download_checks_each_file_against_its_published_md5(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    payloads: dict[str, bytes] = {}
    fetched: list[str] = []

    def fake_download(url, dest, *_args):
        fetched.append(url)
        payloads[url] = f"data of {url}".encode()
        Path(dest).write_bytes(payloads[url])
        return True

    def published_md5(url, timeout):
        assert url.endswith(".md5") and timeout == 60
        return io.BytesIO(f"{_md5(payloads[url[:-4]])}  name\n".encode())

    with (
        patch.object(ar, "download_file", side_effect=fake_download),
        patch.object(ar, "urlopen", side_effect=published_md5),
    ):
        assert ar.download_cadd(str(tmp_path), "hg19") is True
        base = f"{ar.CADD_BASE_URL}/GRCh37"
        indel = f"{base}/gnomad.genomes-exomes.r4.0.indel.tsv.gz"
        snv = f"{base}/whole_genome_SNVs.tsv.gz"
        # The data before its index, indels first.
        assert fetched == [indel, f"{indel}.tbi", snv, f"{snv}.tbi"]
        assert sorted(os.listdir(tmp_path / "ref")) == [
            "cadd_hg19.tsv.gz", "cadd_hg19.tsv.gz.tbi",
            "cadd_indel_hg19.tsv.gz", "cadd_indel_hg19.tsv.gz.tbi",
        ]  # fmt: skip

        # Already installed: nothing is fetched again.
        fetched.clear()
        assert ar.download_cadd(str(tmp_path), "hg19") is True
        assert fetched == []


def test_cadd_download_removes_a_file_that_fails_its_md5(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    def fake_download(url, dest, *_args):
        Path(dest).write_bytes(b"corrupt")
        return True

    with (
        patch.object(ar, "download_file", side_effect=fake_download),
        patch.object(ar, "urlopen", return_value=io.BytesIO(b"0" * 32 + b"  x\n")),
    ):
        assert ar.download_cadd(str(tmp_path), "hg38") is False
    assert os.listdir(tmp_path / "ref") == []

    with patch.object(ar, "download_file", return_value=False):
        assert ar.download_cadd(str(tmp_path), "hg38") is False


PGX_VARIANTS = (
    "Variant ID\tVariant Name\tGene IDs\tGene Symbols\tLocation\tSynonyms\n"
    "PA1\trs200\tPA8\tVKORC1\tNC_000016.10:31096368\tNC_000016.10:g.31096368C>T\n"
)
PGX_CLINICAL = (
    "Clinical Annotation ID\tVariant/Haplotypes\tGene\tLevel of Evidence\t"
    "Phenotype Category\tDrug(s)\n"
    "1\trs200\tVKORC1\t1A\tDosage\twarfarin\n"
)


def _zip_with(dest: str, member: str, text: str) -> None:
    with zipfile.ZipFile(dest, "w") as archive:
        archive.writestr("README.pdf", "x")
        archive.writestr(member, text)


@needs_bcftools
def test_pharmgkb_download_builds_the_hg38_table(tmp_path):
    from wgsextract_cli.core import pharmgkb

    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "pharmgkb_hg38.zip").write_text("old archive")

    def fake_download(url, dest, *_args):
        if url == pharmgkb.CLINICAL_URL:
            _zip_with(dest, "clinical_annotations.tsv", PGX_CLINICAL)
        else:
            _zip_with(dest, "variants.tsv", PGX_VARIANTS)
        return True

    with patch.object(pharmgkb, "download_file", side_effect=fake_download):
        assert pharmgkb.download_pharmgkb(str(tmp_path)) is True
    assert sorted(os.listdir(ref)) == [
        "pharmgkb_hg38.tsv.gz",
        "pharmgkb_hg38.tsv.gz.tbi",
    ]
    with gzip.open(ref / "pharmgkb_hg38.tsv.gz", "rt") as f:
        assert f.read().splitlines() == [
            "#CHROM\tPOS\tREF\tALT\tPHARMGKB",
            "chr16\t31096368\tC\tT\trs200|VKORC1|1A|Dosage|warfarin",
        ]


def test_pharmgkb_download_fails_without_placeable_annotations(tmp_path):
    from wgsextract_cli.core import pharmgkb

    def fake_download(url, dest, *_args):
        if url == pharmgkb.CLINICAL_URL:
            _zip_with(dest, "clinical_annotations.tsv", PGX_CLINICAL)
        else:  # rs200 only on GRCh37
            _zip_with(dest, "variants.tsv", PGX_VARIANTS.replace("16.10", "16.9"))
        return True

    with patch.object(pharmgkb, "download_file", side_effect=fake_download):
        assert pharmgkb.download_pharmgkb(str(tmp_path)) is False
    assert os.listdir(tmp_path / "ref") == []

    with patch.object(pharmgkb, "download_file", return_value=False) as dl:
        assert pharmgkb.download_pharmgkb(str(tmp_path)) is False
    dl.assert_called_once()  # the variants file isn't fetched after a failure


# --- regressions ------------------------------------------------------------


@needs_bcftools
@pytest.mark.parametrize("command", ["clinvar", "spliceai", "gnomad"])
def test_multiallelic_site_gets_each_alleles_vcf_annotation(tmp_path, command):
    # A 1/2 genotype at a site ClinVar (gnomAD, SpliceAI) lists as two records.
    sample = _sample(tmp_path, ["200 multi A G,T . PASS . GT 1/2"])
    values = {
        "clinvar": (("CLNSIG", "CLNDN"), "CLNSIG=Benign;CLNDN=x",
                    "CLNSIG=Pathogenic;CLNDN=y"),
        "spliceai": (("SpliceAI",), "SpliceAI=G|G1|0.01|0|0|0|1|2|3|4",
                     "SpliceAI=T|G1|0.95|0|0|0|1|2|3|4"),
        "gnomad": (("AF",), "AF=0.3", "AF=0.001"),
    }  # fmt: skip
    info, g_value, t_value = values[command]
    source = _vcf(tmp_path / f"{command}.vcf.gz",
                  [f"200 . A G . . {g_value} GT 0/1", f"200 . A T . . {t_value} GT 0/1"],
                  contig="1", info=info)  # fmt: skip
    outdir = tmp_path / "out"
    run_wgsextract("vcf", command, "--input", sample, f"--{command}-file", source,
                   "--outdir", str(outdir))  # fmt: skip
    # Split into one record per ALT, each with its own allele's value.
    tag = {"clinvar": "CLNSIG", "spliceai": "SpliceAI", "gnomad": "GNOMAD_AF"}[command]
    values = [v.split("=", 1)[1].split(";")[0] for v in (g_value, t_value)]
    assert _query(outdir / f"{command}_annotated.vcf.gz", f"%ALT:%{tag}:[%GT]\n") == [
        f"G:{values[0]}:1/0", f"T:{values[1]}:0/1"
    ]  # fmt: skip
    if command == "clinvar":
        # The T allele is Pathogenic: it belongs in the pathogenic subset.
        assert _query(outdir / "clinvar_pathogenic.vcf.gz", "%ALT\n") == ["T"]


@needs_bcftools
@pytest.mark.parametrize("command", ["alphamissense", "revel"])
def test_multiallelic_site_keeps_each_alleles_table_score(tmp_path, command):
    sample = _sample(tmp_path, ["100 multi C T,G . PASS . GT 1/2"])
    if command == "alphamissense":
        table = _tsv(tmp_path / "am.tsv.gz", AM_HEADER, [
            "chr1 100 C T hg38 P1 ENST1 A1V 0.1 likely_benign",
            "chr1 100 C G hg38 P1 ENST1 A1G 0.95 likely_pathogenic",
        ])  # fmt: skip
        option = "--am-file"
    else:
        table = _tsv(tmp_path / "revel.tsv.gz", REVEL_HEADER,
                     ["1 100 100 C T 0.1", "1 100 100 C G 0.95"])  # fmt: skip
        option = "--revel-file"
    outdir = tmp_path / "out"
    run_wgsextract("vcf", command, "--input", sample, option, table,
                   "--outdir", str(outdir), "--min-score", "0.5")  # fmt: skip
    # The G allele scores 0.95: it passes a 0.5 minimum.
    assert _query(outdir / f"{command}_gt_0.5.vcf.gz", "%ALT\n") == ["G"]


@needs_bcftools
def test_phylop_table_leaves_gvcf_reference_blocks_unscored(tmp_path):
    sample = _sample(tmp_path, SAMPLE_ROWS)
    table = _tsv(tmp_path / "phylop.tsv.gz", "#Chr\tStart\tEnd\tScore",
                 ["1 100 100 3.0", "1 300 300 5.5"])  # fmt: skip
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "phylop", "--input", sample, "--phylop-file", table,
                   "--outdir", str(outdir), "--min-score", "2")  # fmt: skip
    assert _query(outdir / "phylop_gt_2.0.vcf.gz", "%ID\n") == ["snv"]


@needs_bcftools
def test_failed_annotation_leaves_no_partial_output(tmp_path):
    rows = [f"{p} v{p} A G . PASS . GT 0/1" for p in range(10, 3000, 3)]
    sample = _sample(tmp_path, rows)
    table_rows = [f"chr1 {p} {p} A G 0.5" for p in range(10, 2900, 3)]
    table = _tsv(tmp_path / "revel.tsv.gz", REVEL_HEADER,
                 [*table_rows, "chr1 2950 2950 A G not_a_number"])  # fmt: skip
    outdir = tmp_path / "out"
    with pytest.raises(WGSExtractError, match="REVEL annotation failed"):
        run_wgsextract("vcf", "revel", "--input", sample, "--revel-file", table,
                       "--outdir", str(outdir))  # fmt: skip
    assert os.listdir(outdir) == []


@needs_bcftools
def test_annotating_an_output_onto_itself_is_refused(tmp_path):
    previous = _vcf(tmp_path / "clinvar_annotated.vcf.gz", SAMPLE_ROWS)
    before = Path(previous).read_bytes()
    clinvar = _vcf(tmp_path / "clinvar_new.vcf.gz",
                   ["100 c1 C T . . CLNSIG=Benign;CLNDN=x GT 0/1"],
                   info=("CLNSIG", "CLNDN"))  # fmt: skip
    with pytest.raises(WGSExtractError, match="would overwrite its input.*--outdir"):
        run_wgsextract("vcf", "clinvar", "--input", previous, "--clinvar-file", clinvar)
    assert Path(previous).read_bytes() == before


@needs_bcftools
def test_interrupted_table_compression_is_redone(tmp_path):
    from wgsextract_cli.commands.vcf import annotation_helpers

    plain = tmp_path / "revel.tsv"
    rows = [f"1\t{p}\t{p}\tA\tG\t0.5" for p in range(1, 2001)]
    plain.write_text("\n".join([REVEL_HEADER, *rows]) + "\n")

    def killed_bgzip(cmd, stdout=None, **_kwargs):
        # bgzip had written the first rows when it was killed.
        head = "\n".join([REVEL_HEADER, *rows[:10]]) + "\n"
        done = subprocess.run([get_tool_path("bgzip"), "-c"], input=head.encode(),
                              capture_output=True, check=True)  # fmt: skip
        stdout.write(done.stdout)
        raise subprocess.CalledProcessError(-9, cmd)

    with (
        patch.object(annotation_helpers, "run_command", side_effect=killed_bgzip),
        pytest.raises(subprocess.CalledProcessError),
    ):
        annotation_helpers.prepare_tabix_annotation(str(plain), "REVEL")
    prepared = annotation_helpers.prepare_tabix_annotation(str(plain), "REVEL")
    with gzip.open(prepared, "rt") as f:
        assert len(f.read().splitlines()) == 1 + len(rows)
