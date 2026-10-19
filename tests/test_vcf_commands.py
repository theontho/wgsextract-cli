"""Focused regressions for vcf/, VEP and analyze command helpers."""

import argparse
import gzip
import os
import random
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError


def _have(*tools: str) -> bool:
    return all(get_tool_path(tool) for tool in tools)


needs_bcftools = pytest.mark.skipif(
    not _have("bcftools", "bgzip", "tabix"), reason="bcftools/htslib missing"
)


def _write_vcf(path: Path, body: str, contig: str = "chr1", sample: str = "") -> str:
    """Write and index a tiny bgzipped VCF; body lines use '{c}' for the contig."""
    fmt = "\tFORMAT\t" + sample if sample else ""
    text = (
        "##fileformat=VCFv4.2\n"
        f"##contig=<ID={contig},length=1000>\n"
        '##INFO=<ID=SpliceAI,Number=.,Type=String,Description="s">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO{fmt}\n" + body.format(c=contig)
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
    assert _query(annotated, "%CHROM:%POS:%CLNSIG\n") == [
        "1:10:Pathogenic",
        "1:20:Benign",
    ]
    assert _query(str(outdir / "clinvar_pathogenic.vcf.gz"), "%POS\n") == ["10"]
    assert sorted(os.listdir(inputs)) == ["sample.vcf.gz", "sample.vcf.gz.tbi"]
    assert sorted(os.listdir(outdir)) == [
        "clinvar_annotated.vcf.gz",
        "clinvar_annotated.vcf.gz.tbi",
        "clinvar_pathogenic.vcf.gz",
        "clinvar_pathogenic.vcf.gz.tbi",
    ]


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


def test_spliceai_download_requires_manual_file(tmp_path):
    from wgsextract_cli.core import annotation_resources as ar

    with patch.object(ar, "download_file") as download:
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
