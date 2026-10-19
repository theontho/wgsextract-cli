import gzip
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wgsextract_cli.commands import (
    _lineage_ydna,
    _microarray_combined,
    _microarray_vcf,
    lineage,
    microarray,
)
from wgsextract_cli.core import variant_files
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command


def test_tool_command_keeps_windows_paths_and_uses_current_python():
    win_path = r"C:\Program Files\haplogrep\haplogrep.exe"
    assert _lineage_ydna.tool_command(win_path) == [win_path]
    assert _lineage_ydna.tool_command("/opt/yleaf/Yleaf.py") == [
        sys.executable,
        "/opt/yleaf/Yleaf.py",
    ]


def test_resolve_lineage_tool_resolves_bare_names_only(tmp_path):
    tool = tmp_path / "haplogrep"
    tool.touch()
    with patch.object(
        _lineage_ydna, "get_tool_path", return_value=str(tool)
    ) as get_tool:
        assert _lineage_ydna.resolve_lineage_tool(
            "haplogrep", "haplogrep", "--x"
        ) == str(tool)
        assert _lineage_ydna.resolve_lineage_tool(str(tool), "haplogrep", "--x") == (
            str(tool)
        )
    get_tool.assert_called_once_with("haplogrep")


def test_match_chromosome_name_handles_aliases():
    assert variant_files.match_chromosome_name("MT", ["chr1", "chrM"]) == "chrM"
    assert variant_files.match_chromosome_name("Y", ["1", "Y"]) == "Y"
    assert variant_files.match_chromosome_name("chrY", ["chr1"]) is None


def _ydna_args(input_path, outdir=None):
    return SimpleNamespace(
        input=str(input_path),
        outdir=outdir,
        ref=None,
        threads=None,
        yleaf_path=None,
        pos_file=None,
        extra_args=None,
    )


def test_ydna_without_outdir_never_points_yleaf_at_the_input_directory(tmp_path):
    bam = tmp_path / "sample.bam"
    bam.touch()
    commands = []

    def fake_yleaf(cmd, **kwargs):
        commands.append((cmd, kwargs))
        if "-o" in cmd:
            outdir = Path(cmd[cmd.index("-o") + 1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "hg_prediction.hg").write_text(
                "Sample_name\tHg\tHg_marker\nsample\tR1b\tR-M269\n"
            )

    with (
        patch.object(_lineage_ydna, "resolve_lineage_tool", return_value="yleaf"),
        patch.object(_lineage_ydna, "log_dependency_info"),
        patch.object(_lineage_ydna, "calculate_bam_md5", return_value=None),
        patch.object(
            _lineage_ydna,
            "ReferenceLibrary",
            return_value=SimpleNamespace(build="hs38DH", fasta=None),
        ),
        patch.object(_lineage_ydna, "run_command", side_effect=fake_yleaf),
    ):
        _lineage_ydna.cmd_ydna(_ydna_args(bam))

    cmd, kwargs = commands[-1]
    # Yleaf rmtree()s its -o folder (it runs with -force).
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "yleaf")
    assert cmd[cmd.index("-rg") + 1] == "hg38"
    assert "YLEAF_DATA_DIR" in kwargs["env"]
    assert bam.exists()


def test_ydna_cram_requires_reference(tmp_path):
    cram = tmp_path / "sample.cram"
    cram.touch()
    with (
        patch.object(_lineage_ydna, "resolve_lineage_tool", return_value="yleaf"),
        patch.object(_lineage_ydna, "log_dependency_info"),
        patch.object(_lineage_ydna, "calculate_bam_md5", return_value=None),
        patch.object(
            _lineage_ydna,
            "ReferenceLibrary",
            return_value=SimpleNamespace(build="hg38", fasta=None),
        ),
        patch.object(_lineage_ydna, "run_command") as run,
        pytest.raises(WGSExtractError, match="--ref is required"),
    ):
        _lineage_ydna.cmd_ydna(_ydna_args(cram, str(tmp_path / "out")))
    run.assert_not_called()


def _mtdna_args(input_path, outdir):
    return SimpleNamespace(
        input=str(input_path),
        outdir=str(outdir),
        ref=None,
        threads=None,
        haplogrep_path=None,
    )


def _patch_mtdna_alignment_env():
    return (
        patch.object(lineage, "resolve_lineage_tool", return_value="haplogrep"),
        patch.object(lineage, "verify_dependencies"),
        patch.object(lineage, "log_dependency_info"),
        patch.object(lineage, "calculate_bam_md5", return_value=None),
        patch.object(
            lineage,
            "ReferenceLibrary",
            return_value=SimpleNamespace(fasta="/ref/hg38.fa"),
        ),
        patch.object(lineage, "get_chr_name", return_value="chrM"),
    )


def test_mtdna_calls_haploid_and_checks_every_pipeline_stage(tmp_path):
    bam = tmp_path / "sample.bam"
    bam.touch()
    out_file = tmp_path / "haplogrep_results.txt"
    pipelines = []

    def fake_run_command(cmd, **_kwargs):
        out_file.write_text("SampleID\tHaplogroup\nsample\tH1\n")

    p1, p2, p3, p4, p5, p6 = _patch_mtdna_alignment_env()
    with (
        p1,
        p2,
        p3,
        p4,
        p5,
        p6,
        patch.object(
            lineage, "run_pipeline", side_effect=lambda st, **_k: pipelines.append(st)
        ),
        patch.object(lineage, "run_command", side_effect=fake_run_command),
    ):
        lineage.cmd_mtdna(_mtdna_args(bam, tmp_path))

    mpileup, call = pipelines[0]
    assert mpileup[:2] == ["bcftools", "mpileup"]
    assert call[:4] == ["bcftools", "call", "--ploidy", "1"]


def test_mtdna_pipeline_failure_raises(tmp_path):
    bam = tmp_path / "sample.bam"
    bam.touch()
    p1, p2, p3, p4, p5, p6 = _patch_mtdna_alignment_env()
    with (
        p1,
        p2,
        p3,
        p4,
        p5,
        p6,
        patch.object(
            lineage,
            "run_pipeline",
            side_effect=WGSExtractError("bcftools mpileup (exit 1)"),
        ),
        patch.object(lineage, "run_command") as run,
        pytest.raises(WGSExtractError, match="mpileup"),
    ):
        lineage.cmd_mtdna(_mtdna_args(bam, tmp_path))
    run.assert_not_called()


def test_extract_vcf_chromosome_indexes_before_region_query(tmp_path):
    vcf = tmp_path / "in.vcf.gz"
    vcf.touch()
    calls = []

    def fake_index(path):
        calls.append(("index", path))
        Path(path + ".tbi").touch()

    with (
        patch.object(_lineage_ydna, "ensure_vcf_indexed", side_effect=fake_index),
        patch.object(_lineage_ydna, "get_vcf_chr_name", return_value="chrM"),
        patch.object(
            _lineage_ydna,
            "run_command",
            side_effect=lambda cmd, **_: (
                calls.append(cmd) or SimpleNamespace(stdout="chrM\t1\n")
            ),
        ),
    ):
        _lineage_ydna.extract_vcf_chromosome(str(vcf), "MT", str(tmp_path / "o.gz"))

    assert calls[0] == ("index", str(vcf))
    assert calls[1][:4] == ["bcftools", "view", "-r", "chrM"]


PLAIN_VCF = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=chr1,length=1000>\n##contig=<ID=chrM,length=16569>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    "chr1\t10\t.\tA\tG\t50\tPASS\t.\nchrM\t73\t.\tA\tG\t50\tPASS\t.\n"
)


@pytest.mark.skipif(not get_tool_path("bcftools"), reason="bcftools not available")
def test_extract_vcf_chromosome_plain_vcf_matches_any_contig_alias(tmp_path):
    vcf = tmp_path / "in.vcf"
    vcf.write_text(PLAIN_VCF)
    out = tmp_path / "mt.vcf.gz"

    _lineage_ydna.extract_vcf_chromosome(str(vcf), "MT", str(out))

    records = run_command(["bcftools", "view", "-H", str(out)], capture_output=True)
    assert [line.split("\t")[0] for line in records.stdout.splitlines()] == ["chrM"]


@pytest.mark.skipif(not get_tool_path("bcftools"), reason="bcftools not available")
def test_extract_vcf_chromosome_raises_when_chromosome_absent(tmp_path):
    vcf = tmp_path / "in.vcf"
    vcf.write_text(PLAIN_VCF)

    with pytest.raises(WGSExtractError, match="no variants on chromosome Y"):
        _lineage_ydna.extract_vcf_chromosome(str(vcf), "Y", str(tmp_path / "y.vcf.gz"))


def _microarray_args(tmp_path):
    input_path = tmp_path / "sample.bam"
    input_path.touch()
    return SimpleNamespace(
        input=str(input_path),
        outdir=str(tmp_path / "out"),
        ref=None,
        ref_vcf_tab=None,
        ploidy_file=None,
        threads=None,
        region=None,
        parallel=False,
        formats="all",
    )


def _run_microarray(tmp_path, lib):
    captured = {}

    def fake_prepare(**kwargs):
        captured.update(kwargs)
        return "combined.vcf.gz", kwargs["ref_vcf_tab"]

    with (
        patch.object(microarray, "verify_dependencies"),
        patch.object(microarray, "log_dependency_info"),
        patch.object(microarray, "get_resource_defaults", return_value=("2", None)),
        patch.object(microarray, "calculate_bam_md5", return_value=None),
        patch.object(microarray, "ReferenceLibrary", return_value=lib),
        patch.object(microarray, "print_warning"),
        patch.object(microarray, "_prepare_microarray_vcf", side_effect=fake_prepare),
        patch.object(microarray, "_write_microarray_combined_kit"),
        patch.object(microarray, "_convert_microarray_outputs"),
    ):
        microarray.run(_microarray_args(tmp_path))
    return captured


def _microarray_lib(tmp_path, **overrides):
    ref_fasta = tmp_path / "ref.fa"
    ref_vcf_tab = tmp_path / "snps.tab.gz"
    ref_fasta.touch()
    ref_vcf_tab.touch()
    values = {
        "fasta": str(ref_fasta),
        "ref_vcf_tab": str(ref_vcf_tab),
        "ploidy_file": None,
        "liftover_chain": None,
        "build": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_microarray_requires_input():
    args = SimpleNamespace(input=None)
    with pytest.raises(WGSExtractError):
        microarray.run(args)


def test_microarray_hg38_without_chain_fails_before_variant_calling(tmp_path):
    with pytest.raises(WGSExtractError, match="liftover chain"):
        _run_microarray(tmp_path, _microarray_lib(tmp_path, build="hg38"))


def test_microarray_unknown_build_calls_diploid(tmp_path):
    captured = _run_microarray(tmp_path, _microarray_lib(tmp_path, build=None))
    assert captured["ploidy_args"] == ["--ploidy", "2"]
    assert captured["base_name"] == "sample"


def test_combined_kit_alignment_mode_doubles_haploid_calls(tmp_path):
    output = "rsY\tchrY\t5\tA\nrsMiss\tchr1\t6\t.\nrsHet\tchr1\t7\tA/G\n"
    with patch.object(
        _microarray_combined,
        "_command_lines",
        side_effect=lambda _cmd, _w: iter(output.splitlines(True)),
    ):
        kit = _microarray_combined._write_microarray_combined_kit(
            args=SimpleNamespace(region=None),
            outdir=str(tmp_path),
            base_name="sample",
            is_vcf=False,
            out_vcf="x.vcf.gz",
            ref_fasta="ref.fa",
            ref_vcf_tab="t.tab",
        )
    rows = Path(kit).read_text().splitlines()
    assert rows[1:] == ["rsY\tY\t5\tAA", "rsMiss\t1\t6\t--", "rsHet\t1\t7\tAG"]


def test_combined_kit_failure_leaves_no_partial_file(tmp_path):
    with (
        patch.object(
            _microarray_combined,
            "_command_lines",
            side_effect=OSError("bcftools query failed"),
        ),
        pytest.raises(WGSExtractError),
    ):
        _microarray_combined._write_microarray_combined_kit(
            args=SimpleNamespace(region=None),
            outdir=str(tmp_path),
            base_name="sample",
            is_vcf=False,
            out_vcf="x.vcf.gz",
            ref_fasta="ref.fa",
            ref_vcf_tab="t.tab",
        )
    assert list(tmp_path.iterdir()) == []


def test_split_snps_fallback_writes_each_chromosome_once(tmp_path):
    tab = tmp_path / "targets.tab.gz"
    with gzip.open(tab, "wt") as handle:
        handle.write("#CHROM\tPOS\tID\n1\t10\trs1\n2\t20\trs2\n1\t30\trs3\n")
    out = tmp_path / "chunks"
    out.mkdir()

    with patch.object(_microarray_vcf, "run_command", side_effect=OSError("tabix")):
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


def test_ydna_empty_prediction_is_an_error(tmp_path):
    bam = tmp_path / "sample.bam"
    bam.touch()

    def empty_prediction(cmd, **kwargs):
        if "-o" in cmd:
            outdir = Path(cmd[cmd.index("-o") + 1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "hg_prediction.hg").write_text("Sample_name\tHg\tHg_marker\n")

    with (
        patch.object(_lineage_ydna, "resolve_lineage_tool", return_value="yleaf"),
        patch.object(_lineage_ydna, "log_dependency_info"),
        patch.object(_lineage_ydna, "calculate_bam_md5", return_value=None),
        patch.object(
            _lineage_ydna,
            "ReferenceLibrary",
            return_value=SimpleNamespace(build="hs38DH", fasta=None),
        ),
        patch.object(_lineage_ydna, "run_command", side_effect=empty_prediction),
        pytest.raises(WGSExtractError, match="could not predict a Y haplogroup"),
    ):
        _lineage_ydna.cmd_ydna(_ydna_args(bam))


def test_mt_haplogroup_resolves_reference_next_to_the_input(tmp_path):
    """Without --ref (as the GUI's ancestry page calls it), the FASTA beside the
    BAM must be found the same way the extract and vcf commands find it."""
    from wgsextract_cli.commands import lineage

    bam = tmp_path / "sample.bam"
    bam.write_bytes(b"")
    seen = {}

    class StopAfterLookup(Exception):
        pass

    def fake_library(ref, md5, input_path=None):
        seen["input_path"] = input_path
        raise StopAfterLookup

    args = SimpleNamespace(
        input=str(bam), outdir=str(tmp_path / "out"), ref=None, haplogrep_path=None
    )
    with (
        patch.object(lineage, "resolve_lineage_tool", return_value="haplogrep"),
        patch.object(lineage, "verify_dependencies"),
        patch.object(lineage, "log_dependency_info"),
        patch.object(lineage, "calculate_bam_md5", return_value=None),
        patch.object(lineage, "ReferenceLibrary", side_effect=fake_library),
        pytest.raises(StopAfterLookup),
    ):
        lineage.cmd_mtdna(args)  # type: ignore[arg-type]
    assert seen["input_path"] == str(bam)


def test_haplogrep_wrapper_is_bypassed_for_its_jar(tmp_path):
    """Bioconda's haplogrep wrapper breaks on paths with spaces; use its jar."""
    from wgsextract_cli.commands import lineage

    env_bin = tmp_path / "Application Support" / "bin"
    env_bin.mkdir(parents=True)
    wrapper = env_bin / "haplogrep"
    wrapper.write_text('#!/bin/bash\njava -cp $CLASS_PATH genepi.App "$@"\n')
    wrapper.chmod(0o755)
    (env_bin / "haplogrep.jar").write_bytes(b"")
    with patch.object(lineage, "verify_dependencies"):
        assert lineage._haplogrep_command(str(wrapper)) == [
            "java",
            "-jar",
            str(env_bin / "haplogrep.jar"),
        ]
    (env_bin / "haplogrep.jar").unlink()
    with patch.object(lineage, "tool_command", return_value=["tool"]) as tool:
        assert lineage._haplogrep_command(str(wrapper)) == ["tool"]
        tool.assert_called_once_with(str(wrapper))
