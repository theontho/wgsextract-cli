import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wgsextract_cli.commands import _microarray_combined, _microarray_vcf, microarray
from wgsextract_cli.core import variant_files
from wgsextract_cli.core.microarray_utils import (
    liftover_hg38_to_hg19,
    write_formatted_line,
)


class FakeProcess:
    def __init__(self, stdout: str = ""):
        self.stdout = io.StringIO(stdout)
        self.returncode = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.stdout.close()
        return False

    def communicate(self):
        return "", ""

    def wait(self):
        return self.returncode


@pytest.mark.parametrize(
    ("build", "expected_ploidy"),
    [
        ("GRCh37", "GRCh37"),
        ("hs37", "GRCh37"),
        ("GRCh38", "GRCh38"),
        ("hs38DH", "GRCh38"),
        ("hs38d1", "GRCh38"),
    ],
)
def test_microarray_uses_diploid_ploidy_for_reference_model_aliases(
    tmp_path, build, expected_ploidy
):
    input_path = tmp_path / "sample.bam"
    ref_fasta = tmp_path / "ref.fa"
    ref_vcf_tab = tmp_path / "snps.vcf.gz"
    input_path.touch()
    ref_fasta.touch()
    ref_vcf_tab.touch()

    args = SimpleNamespace(
        input=str(input_path),
        outdir=str(tmp_path),
        ref=None,
        ref_vcf_tab=None,
        ploidy_file=None,
        threads=None,
        region=None,
        parallel=False,
        formats="all",
    )
    lib = SimpleNamespace(
        fasta=str(ref_fasta),
        ref_vcf_tab=str(ref_vcf_tab),
        ploidy_file=None,
        liftover_chain=None,
        build=build,
    )
    captured = {}

    def fake_prepare_microarray_vcf(**kwargs):
        captured["ploidy_args"] = kwargs["ploidy_args"]
        return str(tmp_path / "sample_combined.vcf.gz")

    with (
        patch.object(microarray, "verify_dependencies"),
        patch.object(microarray, "log_dependency_info"),
        patch.object(microarray, "verify_paths_exist", return_value=True),
        patch.object(microarray, "get_resource_defaults", return_value=("2", None)),
        patch.object(microarray, "calculate_bam_md5", return_value=None),
        patch.object(microarray, "ReferenceLibrary", return_value=lib),
        patch.object(microarray, "print_warning"),
        patch.object(
            microarray,
            "_prepare_microarray_vcf",
            side_effect=fake_prepare_microarray_vcf,
        ),
        patch.object(
            microarray,
            "_write_microarray_combined_kit",
            return_value=str(tmp_path / "sample_CombinedKit.txt"),
        ),
        patch.object(microarray, "_convert_microarray_outputs"),
    ):
        microarray.run(args)

    assert captured["ploidy_args"] == ["--ploidy", expected_ploidy]


def test_combined_kit_vcf_mode_preserves_existing_mt_chromosome(tmp_path):
    ref_fasta = tmp_path / "ref.fa"
    ref_vcf_tab = tmp_path / "targets.tsv"
    ref_fasta.touch()
    (tmp_path / "ref.fa.fai").write_text("MT\t200\t0\t50\t51\n")
    ref_vcf_tab.write_text("#CHROM\tPOS\tID\tREF\nMT\t100\trsMT\tA\n")

    def fake_popen(cmd, **_kwargs):
        if cmd[:2] == ["bcftools", "query"]:
            return FakeProcess("")
        if cmd[:2] == ["samtools", "faidx"]:
            return FakeProcess(">MT:100-100\nA\n")
        if cmd[0] == "cat":
            return FakeProcess(ref_vcf_tab.read_text())
        raise AssertionError(f"unexpected command: {cmd}")

    args = SimpleNamespace(region=None)
    with patch.object(_microarray_combined, "popen", side_effect=fake_popen):
        combined_kit = _microarray_combined._write_microarray_combined_kit(
            args=args,
            outdir=str(tmp_path),
            base_name="sample",
            is_vcf=True,
            out_vcf=str(tmp_path / "hits.vcf.gz"),
            ref_fasta=str(ref_fasta),
            ref_vcf_tab=str(ref_vcf_tab),
        )

    rows = Path(combined_kit).read_text().splitlines()
    assert "rsMT\tMT\t100\tAA" in rows
    assert all("\tMTT\t" not in row for row in rows)


def test_combined_kit_vcf_mode_matches_mito_aliases_for_hits_and_reference(tmp_path):
    ref_fasta = tmp_path / "ref.fa"
    ref_vcf_tab = tmp_path / "targets.tsv"
    ref_fasta.touch()
    (tmp_path / "ref.fa.fai").write_text("chrM\t200\t0\t50\t51\n")
    ref_vcf_tab.write_text("#CHROM\tPOS\tID\nMT\t100\trsHit\nMT\t101\trsRef\n")
    requested_regions = []

    def fake_popen(cmd, **_kwargs):
        if cmd[:2] == ["bcftools", "query"]:
            return FakeProcess("chrM\t100\tA/A\n")
        if cmd[:2] == ["samtools", "faidx"]:
            region_file = Path(cmd[cmd.index("--region-file") + 1])
            requested_regions.extend(region_file.read_text().splitlines())
            return FakeProcess(">chrM:100-100\nA\n>chrM:101-101\nC\n")
        if cmd[0] == "cat":
            return FakeProcess(ref_vcf_tab.read_text())
        raise AssertionError(f"unexpected command: {cmd}")

    args = SimpleNamespace(region=None)
    with patch.object(_microarray_combined, "popen", side_effect=fake_popen):
        combined_kit = _microarray_combined._write_microarray_combined_kit(
            args=args,
            outdir=str(tmp_path),
            base_name="sample",
            is_vcf=True,
            out_vcf=str(tmp_path / "hits.vcf.gz"),
            ref_fasta=str(ref_fasta),
            ref_vcf_tab=str(ref_vcf_tab),
        )

    rows = Path(combined_kit).read_text().splitlines()
    assert requested_regions == ["chrM:100-100", "chrM:101-101"]
    assert "rsHit\tMT\t100\tAA" in rows
    assert "rsRef\tMT\t101\tCC" in rows
    assert all("\tMTT\t" not in row for row in rows)


def test_combined_kit_bam_mode_normalizes_mito_chromosome_names(tmp_path):
    def fake_popen(cmd, **_kwargs):
        assert cmd[:2] == ["bcftools", "query"]
        return FakeProcess(
            "rsMT\tMT\t16519\tA/A\n"
            "rsM\tM\t1\tC/C\n"
            "rsChrM\tchrM\t2\tG/G\n"
            "rs1\tchr1\t3\tT/T\n"
        )

    args = SimpleNamespace(region=None)
    with patch.object(_microarray_combined, "popen", side_effect=fake_popen):
        combined_kit = _microarray_combined._write_microarray_combined_kit(
            args=args,
            outdir=str(tmp_path),
            base_name="sample",
            is_vcf=False,
            out_vcf=str(tmp_path / "sample.vcf.gz"),
            ref_fasta=str(tmp_path / "ref.fa"),
            ref_vcf_tab=str(tmp_path / "targets.tsv"),
        )

    rows = Path(combined_kit).read_text().splitlines()
    assert "rsMT\tMT\t16519\tAA" in rows
    assert "rsM\tMT\t1\tCC" in rows
    assert "rsChrM\tMT\t2\tGG" in rows
    assert "rs1\t1\t3\tTT" in rows
    assert all("\tMTT\t" not in row for row in rows)


def test_23andme_writer_does_not_expand_existing_mt_chromosome():
    out = io.StringIO()

    write_formatted_line(out, "23andMe_V3", "rsMT", "MT", "100", "AA")
    write_formatted_line(out, "23andMe_V3", "rsM", "M", "101", "CC")

    assert out.getvalue().splitlines() == [
        "rsMT\tMT\t100\tAA",
        "rsM\tMT\t101\tCC",
    ]


def test_liftover_accepts_prefixed_chromosome_names(tmp_path):
    chain_file = tmp_path / "hg38ToHg19.over.chain.gz"
    chain_file.touch()
    input_txt = tmp_path / "input.txt"
    output_txt = tmp_path / "output.txt"
    input_txt.write_text(
        "rsChrM\tchrM\t100\tAA\nrsChr1\tchr1\t200\tCC\nrsMT\tMT\t300\tGG\n"
    )
    seen_chroms = []

    class FakeLiftOver:
        def __init__(self, chain_path):
            assert chain_path == str(chain_file)

        def convert_coordinate(self, chrom, pos):
            seen_chroms.append(chrom)
            return [(chrom, pos + 10)]

    with patch("wgsextract_cli.core.microarray_utils.LiftOver", FakeLiftOver):
        liftover_hg38_to_hg19(str(input_txt), str(output_txt), str(chain_file))

    rows = {
        parts[0]: parts
        for parts in (
            line.split("\t") for line in output_txt.read_text().strip().splitlines()
        )
    }
    assert seen_chroms == ["chrM", "chr1", "chrM"]
    assert rows["rsChrM"] == ["rsChrM", "MT", "110", "AA"]
    assert rows["rsChr1"] == ["rsChr1", "1", "210", "CC"]
    assert rows["rsMT"] == ["rsMT", "MT", "310", "GG"]
    assert all(parts[1] != "MTT" for parts in rows.values())


def test_chromosome_rename_mapping_matches_mito_aliases():
    assert variant_files.chromosome_rename_mapping(
        ["chr1", "chrM", "chrUn"], ["1", "MT"]
    ) == [("chr1", "1"), ("chrM", "MT")]
    assert variant_files.chromosome_rename_mapping(["MT"], ["chrM"]) == [("MT", "chrM")]


def test_normalize_vcf_chromosomes_renames_chr_m_to_target_mt(tmp_path):
    input_vcf = tmp_path / "input.vcf.gz"
    input_vcf.touch()
    rename_map = {}

    def fake_run_command(cmd, **_kwargs):
        if cmd[:3] == ["bcftools", "index", "-s"]:
            return SimpleNamespace(stdout="chr1\t100\nchrM\t16569\n", stderr="")
        if "--rename-chrs" in cmd:
            map_path = cmd[cmd.index("--rename-chrs") + 1]
            rename_map["text"] = Path(map_path).read_text()
            return SimpleNamespace(stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    with (
        patch.object(variant_files, "run_command", side_effect=fake_run_command),
        patch(
            "wgsextract_cli.core.dependencies.get_tool_path", return_value="bcftools"
        ),
        patch.object(variant_files, "ensure_vcf_indexed"),
    ):
        normalized = variant_files.normalize_vcf_chromosomes(
            str(input_vcf), ["1", "MT"]
        )

    assert normalized != str(input_vcf)
    assert rename_map["text"].splitlines() == ["chr1 1", "chrM MT"]
    Path(normalized).unlink(missing_ok=True)


def test_parallel_microarray_vcf_concat_uses_natural_chromosome_order(tmp_path):
    args = SimpleNamespace(
        input=str(tmp_path / "sample.bam"),
        parallel=True,
        region=None,
    )
    out_vcf = str(tmp_path / "sample_combined.vcf.gz")
    concat_commands = []

    class FakeFuture:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def submit(self, _func, chrom, _snp_file, chrom_tmp_dir, *_args):
            return FakeFuture(str(Path(chrom_tmp_dir) / f"{chrom}_ann.vcf.gz"))

    def fake_run_command(cmd, **_kwargs):
        if cmd[:2] == ["bcftools", "concat"]:
            concat_commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr=b"")

    chrom_snps = {
        "chr10": str(tmp_path / "snps_chr10.tab"),
        "chr2": str(tmp_path / "snps_chr2.tab"),
        "chr1": str(tmp_path / "snps_chr1.tab"),
        "chrMT": str(tmp_path / "snps_chrMT.tab"),
        "chrY": str(tmp_path / "snps_chrY.tab"),
        "chrX": str(tmp_path / "snps_chrX.tab"),
    }

    with (
        patch.object(_microarray_vcf, "split_snps_by_chrom", return_value=chrom_snps),
        patch("concurrent.futures.ProcessPoolExecutor", FakeExecutor),
        patch.object(_microarray_vcf, "run_command", side_effect=fake_run_command),
        patch.object(_microarray_vcf, "ensure_vcf_indexed"),
    ):
        _microarray_vcf._prepare_microarray_vcf(
            args=args,
            outdir=str(tmp_path),
            base_name="sample",
            is_vcf=False,
            ref_vcf_tab=str(tmp_path / "targets.tsv"),
            region_args=[],
            ploidy_args=["--ploidy", "GRCh37"],
            ref_fasta=str(tmp_path / "ref.fa"),
            threads="2",
            start_vcf=0.0,
            out_vcf=out_vcf,
        )

    assert len(concat_commands) == 1
    concat_inputs = concat_commands[0][5:]
    assert [Path(path).name for path in concat_inputs] == [
        "chr1_ann.vcf.gz",
        "chr2_ann.vcf.gz",
        "chr10_ann.vcf.gz",
        "chrX_ann.vcf.gz",
        "chrY_ann.vcf.gz",
        "chrMT_ann.vcf.gz",
    ]
