"""Focused regressions for vcf/, VEP and analyze command helpers."""

import argparse
import gzip
import os
import random
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError


def _have(*tools: str) -> bool:
    return all(get_tool_path(tool) for tool in tools)


needs_bcftools = pytest.mark.skipif(
    not _have("bcftools", "bgzip", "tabix"), reason="bcftools/htslib missing"
)


def _write_vcf(
    path: Path, body: str, contig: str = "chr1", sample: str = "", header: str = ""
) -> str:
    """Write and index a tiny bgzipped VCF; body lines use '{c}' for the contig."""
    fmt = "\tFORMAT\t" + sample if sample else ""
    text = (
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={contig},length=1000>\n"
        '##INFO=<ID=SpliceAI,Number=.,Type=String,Description="s">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        + header
        + f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO{fmt}\n"
        + body.format(c=contig)
    )
    plain = path.with_suffix("")
    plain.write_text(text)
    with open(path, "wb") as out:
        subprocess.run(
            [get_tool_path("bgzip"), "-c", str(plain)], stdout=out, check=True
        )
    plain.unlink()
    subprocess.run([get_tool_path("tabix"), "-f", "-p", "vcf", str(path)], check=True)
    return str(path)


def _query(vcf: str, fmt: str = "%ID\n") -> list[str]:
    res = subprocess.run(
        [get_tool_path("bcftools"), "query", "-f", fmt, vcf],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.split()


# --- BSD sum (VEP cache checksums) ---------------------------------------------


def _slow_bsd_sum(data: bytes) -> tuple[int, int]:
    checksum = 0
    for byte in data:
        checksum = (checksum >> 1) + ((checksum & 1) << 15)
        checksum = (checksum + byte) & 0xFFFF
    return checksum, -(-len(data) // 1024)


@pytest.mark.parametrize("size", [0, 1, 1023, 1024, 1025, 70_001])
def test_bsd_sum_matches_reference(tmp_path, size):
    from wgsextract_cli.commands import _vep_resources as vr

    data = random.Random(size).randbytes(size)
    path = tmp_path / "blob"
    path.write_bytes(data)
    expected = _slow_bsd_sum(data)

    with patch.object(vr, "_native_bsd_sum", return_value=None):
        assert vr.calculate_bsd_sum(str(path)) == expected
    native = vr._native_bsd_sum(str(path))
    if native is not None:
        assert native == expected
    if os.path.exists("/usr/bin/sum") and os.uname().sysname == "Darwin":
        out = subprocess.run(
            ["/usr/bin/sum", str(path)], capture_output=True, text=True, check=True
        ).stdout.split()
        assert (int(out[0]), int(out[1])) == expected


def test_vep_checksums_parse_three_field_lines(tmp_path):
    from wgsextract_cli.commands._vep_resources import _expected_bsd_sum

    checksums = tmp_path / "CHECKSUMS"
    checksums.write_text(
        "44494 27192563 homo_sapiens_merged_vep_115_GRCh38.tar.gz\n"
        "01402 24680623 homo_sapiens_vep_115_GRCh38.tar.gz\n"
    )
    assert _expected_bsd_sum(str(checksums), "homo_sapiens_vep_115_GRCh38.tar.gz") == (
        1402,
        24680623,
    )
    assert _expected_bsd_sum(str(checksums), "missing.tar.gz") is None


def test_vep_download_failure_exits_nonzero(tmp_path):
    from wgsextract_cli.commands import _vep_resources as vr

    args = argparse.Namespace(
        species="homo_sapiens",
        assembly="GRCh38",
        vep_version="115",
        mirror="uk",
        vep_cache=str(tmp_path),
    )
    with (
        patch.object(vr, "verify_dependencies"),
        patch.object(vr, "download_file", return_value=False) as download,
        pytest.raises(WGSExtractError, match="download failed"),
    ):
        vr.cmd_vep_download(args)
    assert download.call_args.args[0].startswith(
        "https://ftp.ensembl.org/pub/release-115/variation/indexed_vep_cache/"
    )


# --- VEP run / analyze ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/sven/sample.vcf.gz", "snp-indel"),
        ("/data/svs/snps.vcf.gz", "snp-indel"),
        ("out/sv.vcf.gz", "sv"),
        ("out/sample_SVs.vcf", "sv"),
        ("out/pbsv.vcf.gz", "sv"),
        ("out/cnv.vcf.gz", "cnv"),
    ],
)
def test_vcf_type_detection_uses_basename_tokens(path, expected):
    from wgsextract_cli.commands._analyze_workflows import detect_vcf_type

    assert detect_vcf_type(path) == expected


def test_vep_output_paths():
    from wgsextract_cli.commands._vep_run import vep_output_paths

    assert vep_output_paths("/x/clinvar_annotated.vcf.gz", "/o", "vcf") == (
        "/o/clinvar_annotated_vep.vcf",
        "/o/clinvar_annotated_vep.vcf.gz",
    )
    assert vep_output_paths("/x/s.vcf", "/o", "tab") == ("/o/s_vep.txt",) * 2


def test_vep_requires_input():
    from wgsextract_cli.commands._vep_run import cmd_vep

    with pytest.raises(WGSExtractError):
        cmd_vep(argparse.Namespace(input=None))


@needs_bcftools
def test_discovery_spliceai_expression_matches_high_scores(tmp_path):
    from wgsextract_cli.commands._analyze_workflows import SPLICEAI_HIGH_EXPR

    vcf = _write_vcf(
        tmp_path / "s.vcf.gz",
        "{c}\t10\tlow\tA\tG\t.\tPASS\tSpliceAI=G|GENE|0.01|0.00|0.02|0.10|-3|5|0|12\n"
        "{c}\t20\thigh\tA\tG\t.\tPASS\tSpliceAI=G|GENE|0.00|0.85|0.00|0.00|-3|5|0|12\n"
        "{c}\t30\tone\tA\tG\t.\tPASS\tSpliceAI=G|GENE|0.00|0.00|1.00|0.00|-3|5|0|8\n"
        "{c}\t40\tnone\tA\tG\t.\tPASS\t.\n"
        "{c}\t50\tmulti\tA\tG,T\t.\tPASS\t"
        "SpliceAI=G|GENE|0.01|0.0|0.0|0.1|-3|5|0|1,T|GENE|0.90|0.0|0.0|0.1|-3|5|0|1\n",
    )
    out = tmp_path / "f.vcf.gz"
    subprocess.run(
        [get_tool_path("bcftools"), "filter", "-i", SPLICEAI_HIGH_EXPR, "-Oz"]
        + ["-o", str(out), vcf],
        check=True,
    )
    assert _query(str(out)) == ["high", "one", "multi"]


# --- vcf chain-annotate ---------------------------------------------------------


def test_chain_uses_each_steps_known_output(tmp_path):
    from wgsextract_cli.commands.vcf import chain

    input_vcf = tmp_path / "in.vcf.gz"
    input_vcf.write_bytes(b"")
    step_inputs = []

    def fake_run_command(cmd, **_kwargs):
        step_inputs.append(cmd[cmd.index("--input") + 1])
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        # clinvar writes the full annotation, then the (newer) pathogenic subset.
        (outdir / "clinvar_annotated.vcf.gz").write_text("annotated")
        (outdir / "clinvar_pathogenic.vcf.gz").write_text("subset")
        (outdir / "revel_annotated.vcf.gz").write_text("revel")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    class Lib:
        clinvar_vcf = revel_file = "present"

        def __init__(self, *_a, **_k):
            pass

    args = argparse.Namespace(
        input=str(input_vcf),
        vcf_input=None,
        outdir=str(tmp_path / "out"),
        ref=None,
        annotations="clinvar,revel",
        keep_intermediates=False,
    )
    with (
        patch.object(chain, "verify_dependencies"),
        patch.object(chain, "get_tool_path", return_value="bcftools"),
        patch.object(chain, "ReferenceLibrary", Lib),
        patch.object(chain, "ensure_vcf_prepared", side_effect=lambda p: p),
        patch.object(chain, "run_command", side_effect=fake_run_command),
    ):
        chain.cmd_chain_annotate(args)

    assert step_inputs[1].endswith("chain_step_1_clinvar/clinvar_annotated.vcf.gz")
    assert (tmp_path / "out" / "chain_annotated.vcf.gz").read_text() == "revel"
    assert sorted(os.listdir(tmp_path / "out")) == ["chain_annotated.vcf.gz"]


def test_chain_rejects_unknown_or_empty_annotations(tmp_path):
    from wgsextract_cli.commands.vcf import chain

    for annotations in (" , ", "clinvar,clinvr"):
        args = argparse.Namespace(
            input=str(tmp_path / "in.vcf.gz"),
            vcf_input=None,
            outdir=str(tmp_path),
            ref=None,
            annotations=annotations,
        )
        with (
            patch.object(chain, "verify_dependencies"),
            pytest.raises(WGSExtractError),
        ):
            chain.cmd_chain_annotate(args)


@pytest.mark.parametrize("scenario", ["all_skipped", "step_fails"])
def test_chain_fails_instead_of_copying_unannotated_input(tmp_path, scenario):
    from wgsextract_cli.commands.vcf import chain

    input_vcf = tmp_path / "in.vcf.gz"
    input_vcf.write_bytes(b"")

    class Lib:
        clinvar_vcf = None if scenario == "all_skipped" else "present"

        def __init__(self, *_a, **_k):
            pass

    def failing_step(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, "", "boom")

    args = argparse.Namespace(
        input=str(input_vcf),
        vcf_input=None,
        outdir=str(tmp_path / "out"),
        ref=None,
        annotations="clinvar",
        keep_intermediates=False,
    )
    with (
        patch.object(chain, "verify_dependencies"),
        patch.object(chain, "get_tool_path", return_value="bcftools"),
        patch.object(chain, "ReferenceLibrary", Lib),
        patch.object(chain, "ensure_vcf_prepared", side_effect=lambda p: p),
        patch.object(chain, "run_command", side_effect=failing_step),
        pytest.raises(WGSExtractError),
    ):
        chain.cmd_chain_annotate(args)

    assert not (tmp_path / "out" / "chain_annotated.vcf.gz").exists()


# --- vcf trio / filter ----------------------------------------------------------


def test_trio_indices_follow_merge_order_for_duplicate_names():
    from wgsextract_cli.commands.vcf import filter_trio

    samples = {"p": ["S"], "m": ["S"], "f": ["S", "S2"]}
    with patch.object(filter_trio, "get_vcf_samples", side_effect=samples.get):
        assert filter_trio.trio_sample_indices("p", "m", "f") == (0, 1, 2)
    samples["p"] = ["A", "B"]
    with patch.object(filter_trio, "get_vcf_samples", side_effect=samples.get):
        assert filter_trio.trio_sample_indices("p", "m", "f") == (0, 2, 3)


@needs_bcftools
def test_trio_with_same_sample_name_and_mixed_contig_names(tmp_path):
    from wgsextract_cli.commands.vcf import filter_trio

    def trio_vcf(name: str, contig: str, gts: tuple[str, str, str]) -> str:
        body = "".join(
            f"{{c}}\t{pos}\ts{i}\tA\tG\t50\tPASS\t.\tGT\t{gt}\n"
            for i, (pos, gt) in enumerate(zip((10, 20, 30), gts, strict=True), 1)
        )
        return _write_vcf(tmp_path / name, body, contig, sample="S")

    child = trio_vcf("child.vcf.gz", "chr1", ("0/1", "0/1", "1/1"))
    mom = trio_vcf("mom.vcf.gz", "1", ("0/0", "0/1", "0/1"))
    dad = trio_vcf("dad.vcf.gz", "chr1", ("0/0", "0/0", "0/1"))
    before = sorted(os.listdir(tmp_path))
    outdir = tmp_path / "out"
    args = argparse.Namespace(
        proband=child,
        vcf_input=None,
        mother=mom,
        father=dad,
        outdir=str(outdir),
        region=None,
        mode="all",
    )
    with patch.object(filter_trio, "verify_dependencies"):
        filter_trio.cmd_trio(args)

    assert _query(str(outdir / "trio_denovo.vcf.gz")) == ["s1"]
    assert _query(str(outdir / "trio_comphet.vcf.gz")) == ["s2"]
    assert _query(str(outdir / "trio_recessive.vcf.gz")) == ["s3"]
    assert sorted(os.listdir(tmp_path)) == before + ["out"]
    assert not [f for f in os.listdir(outdir) if f.startswith(".")]


def test_gaps_bed_parses_real_nbin_format(tmp_path):
    from wgsextract_cli.commands.vcf.filter_trio import write_gaps_bed

    ref = tmp_path / "hs38.fa.gz"
    (tmp_path / "hs38_nbin.csv").write_text(
        "#Processing Ref Model: hs38.fa.gz with >300bp of N runs\n"
        "#SN\tBinID\tStart\tSize\n"
        "chr1\t1\t0\t10,000\n"
        "chr1\t5\t2,702,781\t43,509\n"
    )
    bed = tmp_path / "gaps.bed"
    assert write_gaps_bed(str(ref), str(bed))
    assert bed.read_text().splitlines() == [
        "chr1\t0\t10000",
        "chr1\t2702781\t2746290",
    ]
    assert not write_gaps_bed(str(tmp_path / "other.fa"), str(bed))


def test_filter_unresolved_gene_fails(tmp_path):
    from wgsextract_cli.commands.vcf import filter_trio

    vcf = tmp_path / "s.vcf.gz"
    vcf.write_text("")

    class Lib:
        fasta = None
        root = str(tmp_path)
        build = "hg38"

        def __init__(self, *_a, **_k):
            pass

    args = argparse.Namespace(
        input=str(vcf), vcf_input=None, outdir=str(tmp_path), ref=None,
        region=None, expr=None, gene="NOT_A_GENE", exclude_near_gaps=False,
    )  # fmt: skip
    with (
        patch.object(filter_trio, "verify_dependencies"),
        patch.object(filter_trio, "log_dependency_info"),
        patch.object(filter_trio, "calculate_bam_md5", return_value=None),
        patch.object(filter_trio, "ReferenceLibrary", Lib),
        pytest.raises(WGSExtractError, match="NOT_A_GENE"),
    ):
        filter_trio.cmd_filter(args)


# --- annotations ----------------------------------------------------------------


@needs_bcftools
def test_clinvar_normalizes_contigs_and_leaves_no_temporaries(tmp_path):
    from wgsextract_cli.commands.vcf import clinvar_revel

    inputs = tmp_path / "in"
    inputs.mkdir()
    sample = _write_vcf(
        inputs / "sample.vcf.gz",
        "{c}\t10\tv1\tA\tG\t.\tPASS\t.\n{c}\t20\tv2\tA\tG\t.\tPASS\t.\n",
    )
    clinvar = tmp_path / "clinvar.vcf.gz"
    plain = (
        "##fileformat=VCFv4.2\n##contig=<ID=1,length=1000>\n"
        '##INFO=<ID=CLNSIG,Number=.,Type=String,Description="s">\n'
        '##INFO=<ID=CLNDN,Number=.,Type=String,Description="d">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t10\tc1\tA\tG\t.\t.\tCLNSIG=Pathogenic;CLNDN=X\n"
        "1\t20\tc2\tA\tG\t.\t.\tCLNSIG=Benign;CLNDN=Y\n"
    )
    with open(clinvar, "wb") as out:
        subprocess.run(
            [get_tool_path("bgzip"), "-c"], input=plain.encode(), stdout=out, check=True
        )
    subprocess.run([get_tool_path("tabix"), "-p", "vcf", str(clinvar)], check=True)

    outdir = tmp_path / "out"
    args = argparse.Namespace(
        input=sample, vcf_input=None, outdir=str(outdir), ref=None,
        clinvar_file=str(clinvar),
    )  # fmt: skip
    with (
        patch.object(clinvar_revel, "verify_dependencies"),
        patch.object(clinvar_revel, "log_dependency_info"),
    ):
        clinvar_revel.cmd_clinvar(args)

    annotated = str(outdir / "clinvar_annotated.vcf.gz")
    # Matched against ClinVar's "1", written back with the sample's "chr1".
    assert _query(annotated, "%CHROM:%POS:%CLNSIG\n") == [
        "chr1:10:Pathogenic",
        "chr1:20:Benign",
    ]
    assert _query(str(outdir / "clinvar_pathogenic.vcf.gz"), "%POS\n") == ["10"]
    assert sorted(os.listdir(inputs)) == ["sample.vcf.gz", "sample.vcf.gz.tbi"]
    assert sorted(os.listdir(outdir)) == [
        "clinvar_annotated.vcf.gz",
        "clinvar_annotated.vcf.gz.tbi",
        "clinvar_pathogenic.vcf.gz",
        "clinvar_pathogenic.vcf.gz.tbi",
    ]


@needs_bcftools
def test_cadd_scores_snvs_and_indels_per_allele(tmp_path):
    from wgsextract_cli.commands.vcf import deleteriousness

    sample = _write_vcf(
        tmp_path / "sample.vcf.gz",
        "{c}\t100\tm\tC\tT,CA\t.\tPASS\t.\n"
        "{c}\t150\tblock\tG\t<NON_REF>\t.\tPASS\t.\n"
        "{c}\t200\ts\tG\tA\t.\tPASS\t.\n"
        "{c}\t300\ti\tTA\tT\t.\tPASS\t.\n"
        "{c}\t400\tn\tG\tC\t.\tPASS\t.\n",
    )

    def cadd_tsv(name: str, rows: str) -> str:
        path = tmp_path / name
        text = "## CADD GRCh38-v1.7\n#Chrom\tPos\tRef\tAlt\tRawScore\tPHRED\n" + rows
        with open(path, "wb") as out:
            subprocess.run(
                [get_tool_path("bgzip"), "-c"],
                input=text.encode(),
                stdout=out,
                check=True,
            )
        return str(path)

    snvs = cadd_tsv("cadd_hg38.tsv.gz", "1\t100\tC\tT\t1.5\t21\n1\t200\tG\tA\t0.5\t5\n")
    indels = cadd_tsv(
        "cadd_indel_hg38.tsv.gz", "1\t100\tC\tCA\t0.9\t9\n1\t300\tTA\tT\t2.5\t25\n"
    )
    outdir = tmp_path / "out"
    args = argparse.Namespace(
        input=sample, vcf_input=None, outdir=str(outdir), ref=None,
        cadd_file=snvs, cadd_indel_file=indels, min_score=20.0,
    )  # fmt: skip
    with (
        patch.object(deleteriousness, "verify_dependencies"),
        patch.object(deleteriousness, "log_dependency_info"),
    ):
        deleteriousness.cmd_cadd(args)

    # Matched against CADD's "1", one value per ALT, back on the sample's "chr1".
    assert _query(
        str(outdir / "cadd_annotated.vcf.gz"), "%CHROM:%ID:%CADD_PHRED\n"
    ) == [
        "chr1:m:21,9",
        "chr1:block:.",
        "chr1:s:5",
        "chr1:i:25",
        "chr1:n:.",
    ]
    assert _query(str(outdir / "cadd_gt_20.0.vcf.gz")) == ["m", "i"]
    assert not [n for n in os.listdir(outdir) if n.startswith(".wgse")]


@needs_bcftools
def test_gerp_scores_variants_by_max_over_ref_span(tmp_path):
    pyBigWig = pytest.importorskip("pyBigWig")
    from wgsextract_cli.commands.vcf import deleteriousness

    bigwig = str(tmp_path / "gerp_hg38.bw")
    bw = pyBigWig.open(bigwig, "w")
    bw.addHeader([("1", 1000)])
    bw.addEntries("1", 99, values=[1.5, 4.25, -2.0, 0.5], span=1, step=1)
    bw.close()
    sample = _write_vcf(
        tmp_path / "sample.vcf.gz",
        "{c}\t100\tsnv\tC\tT\t.\tPASS\t.\n"
        "{c}\t100\tdel\tCAT\tC\t.\tPASS\t.\n"
        "{c}\t102\tblock\tT\t<NON_REF>\t.\tPASS\t.\n"
        "{c}\t500\tnodata\tG\tA\t.\tPASS\t.\n",
    )
    outdir = tmp_path / "out"
    args = argparse.Namespace(
        input=sample, vcf_input=None, outdir=str(outdir), ref=None,
        gerp_file=bigwig, min_score=2.0,
    )  # fmt: skip
    with (
        patch.object(deleteriousness, "verify_dependencies"),
        patch.object(deleteriousness, "log_dependency_info"),
    ):
        deleteriousness.cmd_gerp(args)

    assert _query(str(outdir / "gerp_annotated.vcf.gz"), "%CHROM:%ID:%GERP\n") == [
        "chr1:snv:1.5",
        "chr1:del:4.25",
        "chr1:block:.",
        "chr1:nodata:.",
    ]
    assert _query(str(outdir / "gerp_gt_2.0.vcf.gz")) == ["del"]
    assert not [n for n in os.listdir(outdir) if n.startswith(".wgse")]

    # chr1 is 1,000 bp in the VCF but 2,000 bp in these scores: another build.
    bw = pyBigWig.open(bigwig, "w")
    bw.addHeader([("1", 2000)])
    bw.addEntries("1", 99, values=[1.0], span=1, step=1)
    bw.close()
    with (
        patch.object(deleteriousness, "verify_dependencies"),
        patch.object(deleteriousness, "log_dependency_info"),
        pytest.raises(WGSExtractError, match="different reference"),
    ):
        deleteriousness.cmd_gerp(args)


@needs_bcftools
def test_gnomad_v4_af_table_build_and_annotation(tmp_path):
    from wgsextract_cli.commands.vcf import population
    from wgsextract_cli.core import annotation_resources as ar

    info = (
        '##FILTER=<ID=AC0,Description="a">\n'
        '##INFO=<ID=AF_joint,Number=A,Type=Float,Description="f">\n'
    )
    src = tmp_path / "src"
    src.mkdir()
    _write_vcf(
        src / "chr1.vcf.bgz",
        "{c}\t100\t.\tC\tT\t.\tPASS\tAF_joint=0.25\n"
        "{c}\t100\t.\tC\tG\t.\tPASS\tAF_joint=0.5\n"
        "{c}\t200\t.\tG\tA\t.\tAC0\tAF_joint=0.1\n"  # filtered by gnomAD
        "{c}\t300\t.\tTA\tT\t.\tPASS\tAF_joint=0\n",  # never seen
        header=info,
    )
    _write_vcf(src / "chr2.vcf.bgz", "{c}\t50\t.\tA\tG\t.\tPASS\tAF_joint=0.01\n",
               contig="chr2", header=info)  # fmt: skip
    reflib = tmp_path / "lib"
    url = src.as_uri() + "/{contig}.vcf.bgz"
    with (
        patch.object(ar, "GNOMAD_V4_URL", url),
        patch.object(ar, "GNOMAD_V4_CONTIGS", ["chr1", "chr2", "chr3"]),
        patch.object(ar, "_remote_size", lambda u: 1),
    ):
        # chr3 doesn't exist: the build fails but keeps the finished contigs.
        assert not ar.build_gnomad_af(str(reflib), workers=2, attempts=1)
        parts = reflib / "ref" / ".gnomad_hg38_parts"
        assert sorted(os.listdir(parts)) == ["chr1.tsv.gz", "chr2.tsv.gz"]

        _write_vcf(src / "chr3.vcf.bgz", "", contig="chr3", header=info)
        os.remove(src / "chr1.vcf.bgz")  # resumed, so never read again
        assert ar.build_gnomad_af(str(reflib), workers=2)

    table = reflib / "ref" / "gnomad_hg38.tsv.gz"
    assert not parts.exists() and (reflib / "ref" / "gnomad_hg38.tsv.gz.tbi").exists()
    with gzip.open(table, "rt") as f:
        assert f.read().splitlines()[1:] == [
            "#CHROM\tPOS\tREF\tALT\tAF",
            "chr1\t100\tC\tT\t0.25",
            "chr1\t100\tC\tG\t0.5",
            "chr2\t50\tA\tG\t0.01",
        ]

    sample = _write_vcf(
        tmp_path / "sample.vcf.gz",
        "{c}\t100\tm\tC\tG,T\t.\tPASS\t.\n"
        "{c}\t200\tf\tG\tA\t.\tPASS\t.\n"
        "{c}\t300\tn\tTA\tT\t.\tPASS\t.\n",
        contig="1",
    )
    outdir = tmp_path / "out"
    args = argparse.Namespace(
        input=sample, vcf_input=None, outdir=str(outdir), ref=None,
        gnomad_file=str(table), max_af=0.2,
    )  # fmt: skip
    with (
        patch.object(population, "verify_dependencies"),
        patch.object(population, "log_dependency_info"),
    ):
        population.cmd_gnomad(args)
    assert _query(
        str(outdir / "gnomad_annotated.vcf.gz"), "%CHROM:%ID:%GNOMAD_AF\n"
    ) == ["1:m:0.5,0.25", "1:f:.", "1:n:."]
    assert _query(str(outdir / "gnomad_af_lt_0.2.vcf.gz")) == ["f", "n"]


@needs_bcftools
def test_annotate_vcf_per_contig_threads_match_single_process(tmp_path):
    from wgsextract_cli.commands.vcf.annotation_helpers import annotate_vcf

    # hs38DH's HLA contig names contain ':', which `bcftools -r` can't parse.
    hla = "HLA-A*01:01:01:01"
    contigs = f"##contig=<ID=chr2,length=1000>\n##contig=<ID={hla},length=1000>\n"
    body = "".join(
        f"{c}\t{p}\t{c}_{p}\tA\tG\t.\tPASS\t.\n"
        for c in ("chr1", "chr2", hla)
        for p in (5, 9)
    )
    sample = _write_vcf(tmp_path / "s.vcf.gz", body, header=contigs)
    table = tmp_path / "t.tsv"
    table.write_text("1\t5\tA\tG\t0.1\n1\t9\tA\tG\t0.2\n2\t9\tA\tG\t0.3\n")
    subprocess.run([get_tool_path("bgzip"), "-f", str(table)], check=True)
    subprocess.run(
        [get_tool_path("tabix"), "-s1", "-b2", "-e2", f"{table}.gz"], check=True
    )
    results = []
    for threads in (1, 4):
        out = str(tmp_path / f"out{threads}.vcf.gz")
        annotate_vcf(
            sample, f"{table}.gz", "CHROM,POS,REF,ALT,INFO/X", out, str(tmp_path),
            "X", ['##INFO=<ID=X,Number=A,Type=Float,Description="x">'],
            threads=threads,
        )  # fmt: skip
        results.append(_query(out, "%ID:%X\n"))
    assert results[0] == results[1] == [
        "chr1_5:0.1", "chr1_9:0.2", "chr2_5:.", "chr2_9:0.3", f"{hla}_5:.", f"{hla}_9:."
    ]  # fmt: skip


def test_missing_annotation_resource_raises():
    from wgsextract_cli.commands.vcf.annotation_helpers import require_annotation_file

    with pytest.raises(WGSExtractError, match="wgsextract ref revel"):
        require_annotation_file(None, "vcf_revel_missing", "revel")


def test_cmd_annotate_reads_vcf_header_columns(tmp_path):
    from wgsextract_cli.commands.vcf import basic

    def fake_run_command(cmd, **kwargs):
        assert kwargs.get("capture_output") is True
        return subprocess.CompletedProcess(cmd, 0, "##INFO=<ID=HG,Number=1>\n", "")

    with patch.object(basic, "run_command", side_effect=fake_run_command):
        assert basic._annotation_columns("ann.vcf.gz") == "ID,INFO/HG"


# --- callers --------------------------------------------------------------------


def test_freebayes_cram_region_is_positional(tmp_path):
    from wgsextract_cli.commands.vcf import callers

    cram = tmp_path / "s.cram"
    ref = tmp_path / "ref.fa"
    args = argparse.Namespace(input=str(cram), region="chr20:1-1000")
    stages = []
    with (
        patch.object(callers, "verify_dependencies"),
        patch.object(
            callers, "get_base_args", return_value=("2", str(tmp_path), str(ref), None)
        ),
        patch.object(
            callers, "run_pipeline", side_effect=lambda s, **_k: stages.extend(s)
        ),
        patch.object(callers, "ensure_vcf_indexed"),
    ):
        callers.cmd_freebayes(args)

    view = stages[0]
    assert view[:2] == ["samtools", "view"]
    assert "-r" not in view
    assert view[-2:] == [str(cram), "chr20:1-1000"]


def test_plain_reference_decompresses_to_removed_temp_dir(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("work")
    from wgsextract_cli.commands.vcf.basic import plain_reference

    ref = tmp_path / "ref.fa.gz"
    with gzip.open(ref, "wt") as f:
        f.write(">chr1\nACGT\n")
    with patch(
        "wgsextract_cli.commands.vcf.basic.run_command",
        side_effect=lambda cmd, **_k: Path(cmd[-1] + ".fai").write_text("chr1\t4\n"),
    ):
        with plain_reference(str(ref), str(tmp_path)) as plain:
            assert Path(plain).read_text() == ">chr1\nACGT\n"
            assert os.path.exists(plain + ".fai")
    assert sorted(os.listdir(tmp_path)) == ["ref.fa.gz"]


@pytest.mark.parametrize(
    ("region", "bed"),
    [
        ("chr20", "chr20\t0\t5000"),
        ("chr20:1,001-2,000", "chr20\t1000\t2000"),
        ("chr20:4001", "chr20\t4000\t5000"),
    ],
)
def test_sniffles_region_bed(tmp_path, region, bed):
    from wgsextract_cli.commands.vcf.structural import _write_region_bed

    ref = tmp_path / "ref.fa"
    Path(str(ref) + ".fai").write_text("chr20\t5000\t7\t60\t61\n")
    out = tmp_path / "r.bed"
    _write_region_bed(region, str(ref), str(out))
    assert out.read_text() == bed + "\n"


def test_sniffles_region_bed_rejects_unknown_contig(tmp_path):
    from wgsextract_cli.commands.vcf.structural import _write_region_bed

    ref = tmp_path / "ref.fa"
    Path(str(ref) + ".fai").write_text("chr20\t5000\t7\t60\t61\n")
    with pytest.raises(WGSExtractError):
        _write_region_bed("chrX:1-5", str(ref), str(tmp_path / "r.bed"))


def test_deepvariant_bioconda_rejects_checkpoint(tmp_path):
    from wgsextract_cli.commands.vcf import deepvariant

    args = argparse.Namespace(checkpoint="model.ckpt")
    with (
        patch.object(
            deepvariant,
            "get_tool_path",
            side_effect=lambda t: None if t == "run_deepvariant" else t,
        ),
        pytest.raises(WGSExtractError, match="--checkpoint"),
    ):
        deepvariant.cmd_deepvariant(args)


def test_deepvariant_bioconda_pipeline_uses_temp_workdir(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("work")
    from wgsextract_cli.commands.vcf import deepvariant

    bam = tmp_path / "s.bam"
    ref = tmp_path / "ref.fa"
    out_vcf = tmp_path / "deepvariant.vcf.gz"
    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append((cmd, kwargs))
        if cmd[0] == "dv_postprocess_variants.py":
            Path(cmd[cmd.index("--outfile") + 1]).write_text("vcf")
        return subprocess.CompletedProcess(cmd, 0, "@RG\tID:1\tSM:NA12878\n", "")

    args = argparse.Namespace(
        input=str(bam), region="chr20", checkpoint=None, pacbio=False,
        model_type="HYBRID_PACBIO_ILLUMINA", wes=False,
    )  # fmt: skip
    with (
        patch.object(
            deepvariant,
            "get_tool_path",
            side_effect=lambda t: None if t == "run_deepvariant" else t,
        ),
        patch.object(deepvariant, "verify_dependencies"),
        patch.object(
            deepvariant,
            "get_base_args",
            return_value=("4", str(tmp_path), str(ref), None),
        ),
        patch.object(deepvariant, "run_command", side_effect=fake_run_command),
        patch.object(deepvariant, "ensure_vcf_indexed"),
    ):
        deepvariant.cmd_deepvariant(args)

    dv = [(c, k) for c, k in commands if c[0].startswith("dv_")]
    assert [c[0] for c, _ in dv] == list(deepvariant.BIOCONDA_SCRIPTS)
    make, call, post = (c for c, _ in dv)
    assert make[make.index("--sample") + 1] == "NA12878"
    assert make[-2:] == ["--regions", "chr20"]
    assert call[call.index("--model") + 1] == "hybrid"
    assert post[-1] == str(out_vcf)
    assert all(k["stdin"] == subprocess.DEVNULL for _, k in dv)
    assert "/dev/null" not in " ".join(" ".join(c) for c, _ in dv)
    assert sorted(os.listdir(tmp_path)) == ["deepvariant.vcf.gz"]


# --- annotation resources -------------------------------------------------------


def _fake_downloads(urls):
    from wgsextract_cli.core import annotation_resources as ar

    def fake_download(url, dest, *_args):
        urls.append(url)
        Path(dest).write_bytes(b"\x1f\x8b data" + ar._BGZF_EOF)
        return True

    return fake_download


def _tabix_query(lines, returncode=0, stderr=""):
    query = MagicMock()
    query.stdout = iter(lines)
    query.stderr.read.return_value = stderr
    query.wait.return_value = returncode
    return query


def test_spliceai_downloads_hg38_with_its_index_and_hg19_is_optional(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    urls: list[str] = []
    record = _tabix_query(["Y\t2786855\t.\tC\tA\t.\t.\tSpliceAI=...\n"])
    with (
        patch.object(ar.shutil, "which", return_value="/usr/bin/tabix"),
        patch.object(ar, "download_file", side_effect=_fake_downloads(urls)),
        patch.object(
            ar, "run_command", return_value=MagicMock(stdout="1\n2\nY\n")
        ) as run,
        patch.object(ar, "popen", return_value=record) as query,
    ):
        # hg19 has no open download; its absence is only a warning.
        assert ar.download_spliceai(str(tmp_path)) is True
    hg38 = ar.SPLICEAI_URLS["hg38"]
    assert urls == [hg38, f"{hg38}.tbi"]  # the data before its index
    dest = tmp_path / "ref" / "spliceai_hg38.vcf.gz"
    run.assert_called_once_with(["tabix", "-l", str(dest)], capture_output=True)
    assert query.call_args.args[0] == ["tabix", str(dest), "Y"]
    assert (tmp_path / "ref" / "spliceai_hg38.vcf.gz.tbi").is_file()


def test_spliceai_download_that_fails_its_check_is_removed(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    with (
        patch.object(ar.shutil, "which", return_value="/usr/bin/tabix"),
        patch.object(ar, "download_file", side_effect=_fake_downloads([])),
        patch.object(ar, "run_command", return_value=MagicMock(stdout="1\nY\n")),
        # A readable record, then a read error later in the contig.
        patch.object(
            ar, "popen", return_value=_tabix_query(["Y\t1\n"], 1, "truncated block")
        ),
    ):
        assert ar.download_spliceai(str(tmp_path)) is False
    assert os.listdir(tmp_path / "ref") == []


def test_spliceai_without_tabix_downloads_nothing(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    with (
        patch.object(ar.shutil, "which", return_value=None),
        patch.object(ar, "download_file") as download,
    ):
        assert ar.download_spliceai(str(tmp_path)) is False
    download.assert_not_called()


@pytest.mark.skipif(
    not (shutil.which("bgzip") and shutil.which("tabix")), reason="htslib missing"
)
def test_spliceai_check_reads_a_real_index(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    vcf = tmp_path / "src.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=1>\n##contig=<ID=Y>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t100\t.\tA\tG\t.\t.\tSpliceAI=G|X|0.1|0|0|0|1|2|3|4\n"
        "Y\t200\t.\tC\tT\t.\t.\tSpliceAI=T|Y|0.9|0|0|0|1|2|3|4\n"
    )
    subprocess.run(["bgzip", "-f", str(vcf)], check=True)
    subprocess.run(["tabix", "-p", "vcf", f"{vcf}.gz"], check=True)
    remote = f"{vcf}.gz"

    def copy(url, dest, *_args):
        shutil.copy(url, dest)
        return True

    with (
        patch.dict(ar.SPLICEAI_URLS, {"hg38": remote, "hg19": None}),
        patch.object(ar, "download_file", side_effect=copy),
    ):
        ar.download_spliceai(str(tmp_path / "lib"))
    assert (tmp_path / "lib" / "ref" / "spliceai_hg38.vcf.gz.tbi").is_file()

    # Truncated: the index points past the end of the data.
    Path(remote).write_bytes(Path(remote).read_bytes()[:40])
    for name in ("spliceai_hg38.vcf.gz", "spliceai_hg38.vcf.gz.tbi"):
        (tmp_path / "lib" / "ref" / name).unlink()
    with (
        patch.dict(ar.SPLICEAI_URLS, {"hg38": remote, "hg19": None}),
        patch.object(ar, "download_file", side_effect=copy),
    ):
        ar.download_spliceai(str(tmp_path / "lib"))
    assert not (tmp_path / "lib" / "ref" / "spliceai_hg38.vcf.gz").exists()


@pytest.mark.skipif(
    not (shutil.which("bgzip") and shutil.which("tabix")), reason="htslib missing"
)
def test_spliceai_keeps_files_the_user_put_in_place(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    ref = tmp_path / "ref"
    ref.mkdir()
    # Illumina's file, saved by hand without an index: indexed, not replaced.
    plain = ref / "spliceai_hg38.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=1>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t100\t.\tA\tG\t.\t.\tSpliceAI=G|X|0.1|0|0|0|1|2|3|4\n"
    )
    subprocess.run(["bgzip", "-f", str(plain)], check=True)
    manual = ref / "spliceai_hg38.vcf.gz"
    before = manual.read_bytes()
    with patch.object(ar, "download_file") as download:
        assert ar.download_spliceai(str(tmp_path)) is True
    download.assert_not_called()
    assert manual.read_bytes() == before
    assert (ref / "spliceai_hg38.vcf.gz.tbi").is_file()

    # A pair already in place that fails the check is reported, not removed.
    (ref / "spliceai_hg38.vcf.gz.tbi").write_bytes(b"not an index")
    with patch.object(ar, "download_file") as download:
        assert ar.download_spliceai(str(tmp_path)) is False
    download.assert_not_called()
    assert manual.read_bytes() == before


def test_a_resource_with_no_build_installed_fails(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    with (
        patch.dict(ar.SPLICEAI_URLS, {"hg38": None, "hg19": None}),
        patch.object(ar, "download_file") as download,
    ):
        assert ar.download_spliceai(str(tmp_path)) is False
    download.assert_not_called()


def test_resource_download_rejects_html_payload(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    def fake_download(_url, dest, *_args):
        Path(dest).write_text("<html>login</html>")
        return True

    with patch.object(ar, "download_file", side_effect=fake_download):
        assert ar.download_alphamissense(str(tmp_path)) is False
    assert os.listdir(tmp_path / "ref") == []


@pytest.mark.skipif(not shutil.which("gzip"), reason="gzip missing")
def test_ensure_bgzf_raises_on_tool_failure(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("work")
    from wgsextract_cli.core.annotation_resources import ensure_bgzf

    bogus = tmp_path / "x.tsv.gz"
    bogus.write_text("not gzip at all")
    with pytest.raises(WGSExtractError):
        ensure_bgzf(str(bogus))
    assert os.listdir(tmp_path) == ["x.tsv.gz"]


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ("hg19", ["--ploidy", "GRCh37"]),
        ("hg38", ["--ploidy", "GRCh38"]),
    ],
)
def test_ploidy_falls_back_to_reference_build(build, expected):
    from wgsextract_cli.commands.vcf.basic import _ploidy_args

    lib = argparse.Namespace(ploidy_file=None, build=build)
    args = argparse.Namespace(ploidy=None, ploidy_file=None)
    assert _ploidy_args(args, lib) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("build", ["t2t", None])
def test_ploidy_requires_explicit_value_without_known_build(build):
    from wgsextract_cli.commands.vcf.basic import _ploidy_args

    lib = argparse.Namespace(ploidy_file=None, build=build)
    args = argparse.Namespace(ploidy=None, ploidy_file=None)
    with pytest.raises(WGSExtractError, match="Ploidy"):
        _ploidy_args(args, lib)  # type: ignore[arg-type]


def test_vep_fast_preset_replaces_everything(tmp_path):
    from types import SimpleNamespace

    from wgsextract_cli.commands._vep_run import _build_vep_command

    def command(preset):
        args = SimpleNamespace(
            format="vcf", vep_assembly="GRCh38", vep_cache=str(tmp_path),
            vep_cache_version="116", vep_args=None, preset=preset,
        )  # fmt: skip
        lib = SimpleNamespace(build="hg38", fasta=None, vep_cache=None)
        return _build_vep_command(args, "in.vcf.gz", "out.vcf", "snp-indel", lib, "4")

    assert "--everything" in command("everything")
    fast = command("fast")
    assert "--everything" not in fast
    assert {"--symbol", "--mane", "--sift", "--polyphen"} <= set(fast)
    assert "--af_gnomadg" not in fast and "--hgvs" not in fast


# --- vcf to-fasta ---------------------------------------------------------------

_CONSENSUS_HEADER = (
    '##FILTER=<ID=FAIL,Description="f">\n'
    '##INFO=<ID=END,Number=1,Type=Integer,Description="e">\n'
)


def _consensus(tmp_path: Path, vcf: str, **overrides) -> str:
    from wgsextract_cli.commands.vcf.consensus import cmd_to_fasta

    ref = tmp_path / "ref.fa"
    if not ref.exists():
        ref.write_text(">chr1\n" + "ACGT" * 250 + "\n")
    args = argparse.Namespace(
        vcf_input=vcf,
        input=None,
        outdir=str(tmp_path / "out"),
        ref=str(ref),
        threads="2",
        region=None,
        sample=None,
        haplotype="iupac",
        uncalled="auto",
        include_filtered=False,
        mask=None,
        chain=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    cmd_to_fasta(args)
    out = (
        tmp_path / "out" / (Path(vcf).name.removesuffix(".vcf.gz") + "_consensus.fa.gz")
    )
    with gzip.open(out, "rt") as f:
        return "".join(line.strip() for line in f if not line.startswith(">"))


@needs_bcftools
def test_to_fasta_applies_passing_variants_with_iupac_hets(tmp_path):
    # Reference positions 10/20/30 are C/T/C.
    body = (
        "{c}\t10\t.\tC\tA\t50\tPASS\t.\tGT\t0/1\n"
        "{c}\t20\t.\tT\tG\t50\tPASS\t.\tGT\t1/1\n"
        "{c}\t30\t.\tC\tT\t50\tFAIL\t.\tGT\t1/1\n"
    )
    vcf = _write_vcf(tmp_path / "v.vcf.gz", body, "1", "S", _CONSENSUS_HEADER)
    seq = _consensus(tmp_path, vcf)
    assert len(seq) == 1000
    assert (seq[9], seq[19], seq[29]) == ("M", "G", "C")
    assert "N" not in seq  # variant-only VCF: unlisted sites are reference

    seq = _consensus(tmp_path, vcf, haplotype="alt", include_filtered=True)
    assert (seq[9], seq[19], seq[29]) == ("A", "G", "T")


@needs_bcftools
def test_to_fasta_failure_before_writing_keeps_earlier_output(tmp_path, monkeypatch):
    from wgsextract_cli.commands.vcf import consensus

    body = "{c}\t10\t.\tC\tA\t50\tPASS\t.\tGT\t1/1\n"
    vcf = _write_vcf(tmp_path / "k.vcf.gz", body, "chr1", "S", _CONSENSUS_HEADER)
    assert _consensus(tmp_path, vcf)[9] == "A"

    def fail(*_args):
        raise WGSExtractError("boom")

    monkeypatch.setattr(consensus, "_prepare_vcf", fail)
    with pytest.raises(WGSExtractError, match="boom"):
        _consensus(tmp_path, vcf)
    out = tmp_path / "out" / "k_consensus.fa.gz"
    assert out.exists() and Path(f"{out}.fai").exists()


@needs_bcftools
def test_to_fasta_masks_sites_missing_from_gvcf(tmp_path):
    from wgsextract_cli.commands.vcf.consensus import is_gvcf

    body = (
        "{c}\t1\t.\tA\t<NON_REF>\t.\tPASS\tEND=100\tGT\t0/0\n"
        "{c}\t102\t.\tC\tG\t50\tPASS\t.\tGT\t1/1\n"
    )
    vcf = _write_vcf(tmp_path / "g.vcf.gz", body, "chr1", "S", _CONSENSUS_HEADER)
    assert is_gvcf(vcf)
    seq = _consensus(tmp_path, vcf, region="1:1-110")
    assert seq == ("ACGT" * 25) + "N" + "G" + "N" * 8


@needs_bcftools
def test_to_fasta_needs_sample_for_multisample_vcf(tmp_path):
    body = "{c}\t10\t.\tC\tA\t50\tPASS\t.\tGT\t0/1\t1/1\n"
    vcf = _write_vcf(tmp_path / "m.vcf.gz", body, "chr1", "S1\tS2", _CONSENSUS_HEADER)
    with pytest.raises(WGSExtractError, match="--sample"):
        _consensus(tmp_path, vcf)
    assert _consensus(tmp_path, vcf, sample="S2")[9] == "A"


@needs_bcftools
def test_to_fasta_fixes_n_ref_at_reference_iupac_code(tmp_path):
    # Callers write REF N where the reference has an IUPAC code (hs38 chr7 R).
    (tmp_path / "ref.fa").write_text(
        ">chr1\n" + "ACGTACGT" + "R" + "CGT" + "ACGT" * 247 + "\n"
    )
    body = (
        "{c}\t9\t.\tN\tNTT\t50\tPASS\t.\tGT\t0/1\n"
        "{c}\t13\t.\tA\tG\t50\tPASS\t.\tGT\t1/1\n"
        "chrUn\t5\t.\tA\tG\t50\tPASS\t.\tGT\t1/1\n"
    )
    header = _CONSENSUS_HEADER + "##contig=<ID=chrUn,length=50>\n"
    vcf = _write_vcf(tmp_path / "n.vcf.gz", body, "chr1", "S", header)
    seq = _consensus(tmp_path, vcf, haplotype="alt")
    assert seq.startswith("ACGTACGT" + "RTT" + "CGT" + "G" + "CGT")


@needs_bcftools
def test_to_fasta_rejects_vcf_from_another_build(tmp_path):
    (tmp_path / "ref.fa").write_text(">chr1\n" + "ACGT" * 200 + "\n")
    body = "{c}\t10\t.\tC\tA\t50\tPASS\t.\tGT\t0/1\n"
    vcf = _write_vcf(tmp_path / "b.vcf.gz", body, "1", "S", _CONSENSUS_HEADER)
    with pytest.raises(WGSExtractError, match="different reference"):
        _consensus(tmp_path, vcf)
