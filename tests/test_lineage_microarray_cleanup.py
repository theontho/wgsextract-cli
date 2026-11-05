"""Lineage and microarray helpers: tool resolution, chromosome aliases,
CombinedKit writing, sex checks and partial-output cleanup."""

import gzip
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.htslib_helpers import needs_htslib, needs_tools, run_wgsextract, sam_to_bam
from wgsextract_cli.commands import (
    _lineage_ydna,
    _microarray_combined,
    _microarray_vcf,
    lineage,
    microarray,
)
from wgsextract_cli.commands.info import metrics
from wgsextract_cli.core import variant_files
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.pixi_tools import _join_command
from wgsextract_cli.core.tool_catalog import JAVA_JAR
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import calculate_bam_md5


def test_tool_command_keeps_windows_paths_and_uses_current_python():
    win_path = r"C:\Program Files\haplogrep\haplogrep.exe"
    assert _lineage_ydna.tool_command(win_path) == [win_path]
    assert _lineage_ydna.tool_command("/opt/yleaf/Yleaf.py") == [
        sys.executable,
        "/opt/yleaf/Yleaf.py",
    ]


@needs_tools("samtools")
def test_resolve_lineage_tool_resolves_bare_names_only(tmp_path):
    tool = tmp_path / "haplogrep"
    tool.touch()
    assert _lineage_ydna.resolve_lineage_tool(str(tool), "haplogrep", "--x") == str(
        tool
    )
    # A bare command name is looked up like any installed tool.
    assert _lineage_ydna.resolve_lineage_tool("samtools", "haplogrep", "--x") == (
        get_tool_path("samtools")
    )


def test_resolve_lineage_tool_accepts_resolved_wrapper_commands(tmp_path):
    """On Windows Haplogrep resolves to a `pixi run ... java -jar` command, which
    isn't a file; only a path the user passed is checked for existence."""
    wrapper = (
        '"C:\\Users\\lab user\\.pixi\\bin\\pixi.exe" run -e default java -jar "h.jar"'
    )
    with (
        patch.object(_lineage_ydna, "verify_dependencies"),
        patch.object(_lineage_ydna, "get_tool_path", return_value=wrapper),
    ):
        assert _lineage_ydna.resolve_lineage_tool(None, "haplogrep", "--x") == wrapper
    with pytest.raises(WGSExtractError):
        _lineage_ydna.resolve_lineage_tool(
            str(tmp_path / "missing" / "haplogrep"), "haplogrep", "--x"
        )


def test_match_chromosome_name_handles_aliases():
    assert variant_files.match_chromosome_name("MT", ["chr1", "chrM"]) == "chrM"
    assert variant_files.match_chromosome_name("Y", ["1", "Y"]) == "Y"
    assert variant_files.match_chromosome_name("chrY", ["chr1"]) is None


PLAIN_VCF = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chr1,length=1000>\n##contig=<ID=chrM,length=16569>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "chr1\t10\t.\tA\tG\t50\tPASS\t.\nchrM\t73\t.\tA\tG\t50\tPASS\t.\n"
)


@needs_tools("bcftools")
def test_extract_vcf_chromosome_plain_vcf_matches_any_contig_alias(tmp_path):
    vcf = tmp_path / "in.vcf"
    vcf.write_text(PLAIN_VCF)
    out = tmp_path / "mt.vcf.gz"

    _lineage_ydna.extract_vcf_chromosome(str(vcf), "MT", str(out))

    records = run_command(["bcftools", "view", "-H", str(out)], capture_output=True)
    assert [line.split("\t")[0] for line in records.stdout.splitlines()] == ["chrM"]


@needs_tools("bcftools", "bgzip", "tabix")
def test_extract_vcf_chromosome_indexes_a_bgzipped_vcf_for_its_region(tmp_path):
    plain = tmp_path / "in.vcf"
    plain.write_text(PLAIN_VCF)
    vcf = tmp_path / "in.vcf.gz"
    with open(vcf, "wb") as f:
        run_command([get_tool_path("bgzip"), "-c", str(plain)], stdout=f)
    out = tmp_path / "mt.vcf.gz"

    assert _lineage_ydna.extract_vcf_chromosome(str(vcf), "MT", str(out)) == "chrM"

    assert Path(f"{vcf}.tbi").exists() or Path(f"{vcf}.csi").exists()
    records = run_command(["bcftools", "view", "-H", str(out)], capture_output=True)
    assert [line.split("\t")[:2] for line in records.stdout.splitlines()] == [
        ["chrM", "73"]
    ]


@needs_tools("bcftools")
def test_extract_vcf_chromosome_raises_when_chromosome_absent(tmp_path):
    vcf = tmp_path / "in.vcf"
    vcf.write_text(PLAIN_VCF)

    with pytest.raises(WGSExtractError, match="no variants on chromosome Y"):
        _lineage_ydna.extract_vcf_chromosome(str(vcf), "Y", str(tmp_path / "y.vcf.gz"))


def test_microarray_requires_input():
    args = SimpleNamespace(input=None)
    with pytest.raises(WGSExtractError):
        microarray.run(args)


@needs_tools("bcftools")
def test_combined_kit_alignment_mode_doubles_haploid_calls(tmp_path):
    vcf = tmp_path / "calls.vcf"
    vcf.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1>\n##contig=<ID=chrY>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        "chrY\t5\trsY\tA\t.\t50\tPASS\t.\tGT\t0\n"
        "chr1\t6\trsMiss\tA\t.\t50\tPASS\t.\tGT\t./.\n"
        "chr1\t7\trsHet\tA\tG\t50\tPASS\t.\tGT\t0/1\n"
    )
    kit = _microarray_combined._write_microarray_combined_kit(
        args=SimpleNamespace(region=None),
        outdir=str(tmp_path),
        base_name="sample",
        is_vcf=False,
        out_vcf=str(vcf),
        ref_fasta="ref.fa",
        ref_vcf_tab="t.tab",
    )
    rows = Path(kit).read_text().splitlines()
    assert rows[1:] == ["rsY\tY\t5\tAA", "rsMiss\t1\t6\t--", "rsHet\t1\t7\tAG"]


@needs_tools("bcftools")
def test_combined_kit_failure_leaves_no_partial_file(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    with pytest.raises(WGSExtractError):
        _microarray_combined._write_microarray_combined_kit(
            args=SimpleNamespace(region=None),
            outdir=str(outdir),
            base_name="sample",
            is_vcf=False,
            out_vcf=str(tmp_path / "missing.vcf.gz"),
            ref_fasta="ref.fa",
            ref_vcf_tab="t.tab",
        )
    assert list(outdir.iterdir()) == []


@needs_tools("tabix")
def test_split_snps_fallback_writes_each_chromosome_once(tmp_path):
    tab = tmp_path / "targets.tab.gz"
    with gzip.open(tab, "wt") as handle:
        handle.write("#CHROM\tPOS\tID\n1\t10\trs1\n2\t20\trs2\n1\t30\trs3\n")
    out = tmp_path / "chunks"
    out.mkdir()

    # Plain gzip, which tabix can't read, forces the linear scan.
    for _ in range(2):  # re-running must not duplicate rows
        files = _microarray_vcf.split_snps_by_chrom(str(tab), str(out))

    assert Path(files["1"]).read_text() == "1\t10\trs1\n1\t30\trs3\n"
    assert Path(files["2"]).read_text() == "2\t20\trs2\n"


def test_ensure_vcf_prepared_never_leaves_truncated_gz(tmp_path):
    vcf = tmp_path / "in.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n")

    def failing_bgzip(cmd, **_kwargs):
        raise OSError("disk full")

    with (
        patch("wgsextract_cli.core.dependencies.get_tool_path", return_value="bgzip"),
        patch.object(variant_files, "run_command", side_effect=failing_bgzip),
        pytest.raises(OSError),
    ):
        variant_files.ensure_vcf_prepared(str(vcf))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.vcf"]


def test_ensure_vcf_prepared_requires_bgzip(tmp_path):
    vcf = tmp_path / "in.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n")
    with (
        patch("wgsextract_cli.core.dependencies.get_tool_path", return_value=None),
        pytest.raises(WGSExtractError, match="bgzip"),
    ):
        variant_files.ensure_vcf_prepared(str(vcf))


def test_haplogrep_wrapper_is_bypassed_for_its_jar(tmp_path):
    """Bioconda's haplogrep3 wrapper breaks on paths with spaces; use its jar."""

    env_bin = tmp_path / "Application Support" / "bin"
    env_bin.mkdir(parents=True)
    wrapper = env_bin / "haplogrep3"
    wrapper.write_text('#!/bin/bash\njava -cp $CLASS_PATH genepi.App "$@"\n')
    wrapper.chmod(0o755)
    jar = env_bin / "haplogrep3.jar"
    jar.write_bytes(b"")
    with patch.object(lineage, "verify_dependencies"):
        assert lineage._jar_command(str(wrapper), jar.name) == [*JAVA_JAR, str(jar)]
    jar.unlink()
    assert lineage._jar_command(str(wrapper), jar.name) == [str(wrapper)]


def test_a_resolved_haplogrep_command_runs_as_it_is(tmp_path):
    """The resolved tool may already be `java -jar <jar>`: no second `java -jar`.
    It is split, so haplocheck's mutserve.jar can run the same way."""

    jar = tmp_path / "bin" / "haplogrep3.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"")
    command = _join_command([str(tmp_path / "java"), "-jar", str(jar)])
    with patch.object(lineage, "verify_dependencies"):
        assert lineage._jar_command(command, jar.name) == [
            str(tmp_path / "java"),
            "-jar",
            str(jar),
        ]
        assert lineage._jar_command(str(jar), jar.name) == [*JAVA_JAR, str(jar)]


def test_windows_wrapper_commands_keep_backslashes():
    """Windows `pixi run` wrappers are list2cmdline-quoted; paths without spaces
    stay unquoted, so POSIX splitting would strip their backslashes."""
    from wgsextract_cli.core import runtime
    from wgsextract_cli.core.utils import _normalize_subprocess_cmd

    wrapper = (
        r"C:\Users\bob\.pixi\bin\pixi.exe run --manifest-path C:\Users\bob\w\pixi.toml"
        r" -e default java -jar C:\Users\bob\w\haplogrep3.jar"
    )
    with patch.object(runtime, "is_windows_host", return_value=True):
        argv = _normalize_subprocess_cmd([wrapper, "classify"])
    assert argv[0] == r"C:\Users\bob\.pixi\bin\pixi.exe"
    assert argv[3] == r"C:\Users\bob\w\pixi.toml"
    assert argv[-2:] == [r"C:\Users\bob\w\haplogrep3.jar", "classify"]


def _idx(x_reads, y_reads):
    return [
        {"name": "chrX", "length": 156_040_895, "mapped": x_reads, "unmapped": 0},
        {"name": "chrY", "length": 57_227_415, "mapped": y_reads, "unmapped": 0},
    ]


@pytest.mark.parametrize(
    ("x_reads", "y_reads", "sex"),
    [
        (33_392_449, 674_020, "Female"),
        (16_000_000, 6_000_000, "Male"),
        (0, 50, "Male"),  # chrY alone: a Y extract
        (0, 0, "Unknown"),
    ],
)
def test_infer_sex_from_x_and_y_reads(x_reads, y_reads, sex):
    from wgsextract_cli.commands.info.metrics import infer_sex

    assert infer_sex(_idx(x_reads, y_reads)) == sex


def _xy_bam(path: Path, x_reads: int, y_reads: int) -> str:
    """A BAM with hg38's chrX/chrY lengths and this many reads on each."""
    read = "\t60\t50M\t*\t0\t0\t" + "A" * 50 + "\t" + "I" * 50
    return sam_to_bam(
        ["@SQ\tSN:chrX\tLN:156040895", "@SQ\tSN:chrY\tLN:57227415"]
        + [f"x{i}\t0\tchrX\t{100 + i}{read}" for i in range(x_reads)]
        + [f"y{i}\t0\tchrY\t{100 + i}{read}" for i in range(y_reads)],
        path,
    )


@needs_htslib
def test_y_haplogroup_refuses_a_female_sample_before_yleaf(tmp_path):
    """A female genome's mismapped chrY reads made Yleaf "predict" BT* with a full
    QC score; the command must say there is no Y instead."""
    bam = _xy_bam(tmp_path / "sample.bam", x_reads=30, y_reads=1)

    with pytest.raises(WGSExtractError, match="appears female"):
        run_wgsextract("lineage", "y-haplogroup", "--input", bam)
    assert not (tmp_path / "yleaf").exists()


def _info_cache(
    bam: str, call: str, size_delta: int = 0, md5: str | None = None
) -> None:
    st = Path(bam).stat()
    Path(f"{bam}.wgse_info.json").write_text(
        json.dumps({
            "md5_signature": md5 or calculate_bam_md5(bam, None), "gender": call,
            "sex_inference_version": metrics.SEX_INFERENCE_VERSION,
            "input_signature": {
                "size": st.st_size + size_delta, "mtime_ns": st.st_mtime_ns
            },
        })
    )  # fmt: skip


@needs_htslib
def test_sample_sex_trusts_only_this_files_info_cache(tmp_path):
    this_file = _xy_bam(tmp_path / "a.bam", x_reads=30, y_reads=10)
    _info_cache(this_file, "Female")
    assert metrics.sample_sex(this_file) == "Female"

    # A cache for another file of the same name is ignored.
    other_file = _xy_bam(tmp_path / "b.bam", x_reads=30, y_reads=10)
    _info_cache(other_file, "Female", size_delta=1)
    assert metrics.sample_sex(other_file) == "Male"

    # So is one whose size and mtime match but whose header signature doesn't.
    _info_cache(other_file, "Female", md5="0" * 32)
    assert metrics.sample_sex(other_file) == "Male"
