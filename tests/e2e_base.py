"""Shared E2E command suite. Subclasses choose a REGION (chrM) or the whole genome.

Inputs come from the input_path/reference_fasta settings in the real config.toml;
when those are missing (or with ``pytest --fake-data``) the suite runs against
synthetic data generated into out/fake_30x with an isolated, empty config.
"""

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar, TypedDict

import pytest

from tests.smoke_utils import REPO_ROOT, ensure_fake_data, isolate_home, vep_cache_dir
from wgsextract_cli.core.config import reload_settings, settings
from wgsextract_cli.core.dependencies import (
    PIXI_TOOL_ENVS,
    get_tool_path,
    is_pixi_tool_command,
)
from wgsextract_cli.core.utils import run_command, run_pipeline
from wgsextract_cli.core.variant_files import ensure_vcf_indexed
from wgsextract_cli.core.warnings import EXPECTED_TIME
from wgsextract_cli.main import main


def optional_tool_installed(tool: str) -> bool:
    """Whether an opt-in tool is installed, not just installable. For a tool in its
    own Pixi environment, get_tool_path also returns a `pixi run -e <env>` command
    that installs the environment on first use, so require the environment too."""
    command = get_tool_path(tool)
    if not command:
        return False
    if not is_pixi_tool_command(command):
        return True
    return (REPO_ROOT / ".pixi" / "envs" / PIXI_TOOL_ENVS[tool]).is_dir()


FAKE_DIR = REPO_ROOT / "out" / "fake_30x"
OUT_ROOT = REPO_ROOT / "out" / "tests" / "e2e"

# Reference timings from a 30x WGS (7.3GB CRAM) on M1 Pro class hardware.
M1_PRO_ESTIMATES = {
    "GetBAMHeader": 0.03,
    "LiftoverCleanup": 0.01,
    "ButtonBAMStats": 0.04,
    "ButtonBAMStats2": 50.53,
    "CoverageStatsPoz": 49.09,
    "GenBAMIndex": 1.98,
    "BAMtoCRAM": 11.69,
    "ButtonMitoBAM": 2.77,
    "ButtonYonly": 3.03,
    "ButtonUnmappedReads": 0.08,
    "ButtonSNPVCF": 6085.92,
    "ButtonInDelVCF": 6942.84,
    "ButtonCombinedKit": 470.79,
    "ButtonFastp": 1.69,
    "ButtonFastqc": 5.65,
    "CoverageStatsBIN": 3547.49,
    "CoverageStatsWES": 177.89,
    "CreateAlignIndices": 0.03,
    "ButtonAlignBAM": 20.30,
    "GenSortedBAM": 858.75,
    "CRAMtoBAM": 234.73,
    "ButtonUnalignBAM": 1907.09,
    "GenBAMSubset": 391.29,
}


class E2EResult(TypedDict):
    name: str
    success: bool
    duration: float
    expected_key: str


def _prepare_fake_data() -> tuple[str, str]:
    ensure_fake_data(str(FAKE_DIR))
    ref = FAKE_DIR / "fake_ref.fa"
    # A gene map for `vcf filter --gene`.
    gene_map = FAKE_DIR / "ref" / "genes_hg38.tsv"
    if not gene_map.exists():
        gene_map.parent.mkdir(parents=True, exist_ok=True)
        gene_map.write_text("symbol\tchrom\tstart\tend\nBRCA1\tchr1\t1\t1000000\n")
    return str(ref), str(FAKE_DIR / "fake.bam")


def _print_report(mode: str, results: list[E2EResult]) -> None:
    if not results:
        return
    width = 92
    print("\n" + "=" * width)
    print(f"{f'E2E {mode} EXECUTION & BENCHMARK REPORT':^{width}}")
    print("=" * width)
    print(
        f"{'Command':<28} | {'Status':<6} | {'Duration':>10} | {'M1 Pro Ref':>10} | {'Expected':>10}"
    )
    print("-" * width)
    for res in sorted(results, key=lambda r: r["name"]):
        m1_ref = M1_PRO_ESTIMATES.get(res["expected_key"])
        print(
            f"{res['name']:<28} | {'PASS' if res['success'] else 'FAIL':<6} | "
            f"{res['duration']:>9.2f}s | "
            f"{f'{m1_ref:.2f}s' if m1_ref is not None else 'N/A':>10} | "
            f"{EXPECTED_TIME.get(res['expected_key'], 0):>9}s"
        )
    print("-" * width)
    passed = sum(r["success"] for r in results)
    total = sum(r["duration"] for r in results)
    print(f"TOTAL ({mode}): {passed}/{len(results)} passed in {total:.2f}s")
    print("=" * width)


@pytest.mark.real_config
class E2EBase:
    REGION: ClassVar[str | None] = "chrM"
    MODE: ClassVar[str] = "CHRM"
    __test__ = False

    ref: ClassVar[str]
    input: ClassVar[str]
    fake: ClassVar[bool]
    results: ClassVar[list[E2EResult]]

    @pytest.fixture(autouse=True, scope="class")
    @classmethod
    def _e2e_inputs(
        cls,
        pytestconfig: pytest.Config,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> Iterator[None]:
        ref = settings.get("reference_fasta")
        input_path = settings.get("input_path")
        cls.fake = bool(pytestconfig.getoption("fake_data")) or not (
            ref and input_path and os.path.exists(ref) and os.path.exists(input_path)
        )
        cls.results = []
        with pytest.MonkeyPatch.context() as monkeypatch:
            if cls.fake:
                # Synthetic runs must not pick up the developer's config.toml.
                isolate_home(monkeypatch, tmp_path_factory.mktemp("e2e_home"))
                reload_settings()
                ref, input_path = _prepare_fake_data()
            cls.ref, cls.input = ref, input_path
            print(f"\n!!! {cls.MODE} mode, {'synthetic' if cls.fake else 'real'} data")
            print(f"!!! Reference: {ref}\n!!! Input:     {input_path}")
            yield
        _print_report(cls.MODE, cls.results)

    def run_real(
        self,
        name: str,
        args: list[str],
        expected_key: str,
        min_duration: float = 0,
        input_path: str | None = None,
    ) -> None:
        """Run a CLI command in-process and fail unless it exits 0.

        ``{input}``/``{ref}`` in ``args`` are replaced with the suite's inputs; the
        input is symlinked into a fresh output dir so nothing is written next to it.
        """
        current_input = input_path or self.input
        if self.REGION is None:
            name = name.replace("(chrM)", "(Full)")
        safe_name = "".join(c if c.isalnum() else "_" for c in name).strip("_")
        test_dir = OUT_ROOT / self.MODE.replace(" ", "_").lower() / safe_name
        shutil.rmtree(test_dir, ignore_errors=True)
        test_dir.mkdir(parents=True)

        suffixes = "".join(Path(current_input).suffixes[-2:])
        ext = suffixes if suffixes.endswith(".gz") else Path(current_input).suffix
        isolated_input = test_dir / f"input_isolated{ext}"
        isolated_input.symlink_to(current_input)
        for idx_ext in (".bai", ".crai", ".tbi", ".csi"):
            if os.path.exists(current_input + idx_ext):
                Path(f"{isolated_input}{idx_ext}").symlink_to(current_input + idx_ext)

        args = [
            str(isolated_input)
            if arg == "{input}"
            else self.ref
            if arg == "{ref}"
            else arg
            for arg in args
        ]
        region_flags = {"-r", "--region"}
        if self.REGION is None:
            args = [
                arg
                for i, arg in enumerate(args)
                if arg not in region_flags
                and (i == 0 or args[i - 1] not in region_flags)
            ]
        else:
            args = [
                self.REGION if i > 0 and args[i - 1] in region_flags else arg
                for i, arg in enumerate(args)
            ]

        needs_index = bool(region_flags & set(args)) or args[0] in {
            "extract",
            "microarray",
        }
        if (
            needs_index
            and current_input.endswith((".bam", ".cram"))
            and not any(
                Path(f"{isolated_input}{e}").exists() for e in (".bai", ".crai")
            )
        ):
            run_command(["samtools", "index", str(isolated_input)])

        argv = ["--outdir", str(test_dir), *args]
        print(f"\n>>> BEGIN: {name} (expected ~{EXPECTED_TIME.get(expected_key, 0)}s)")
        start = time.perf_counter()
        exit_code: int | str | None = 0
        try:
            main(argv)
        except SystemExit as e:
            exit_code = e.code
        duration = time.perf_counter() - start
        success = exit_code in (0, None) and (self.fake or duration >= min_duration)
        self.results.append(
            {
                "name": name,
                "success": success,
                "duration": duration,
                "expected_key": expected_key,
            }
        )
        print(f"<<< DONE: {name} ({'PASS' if success else 'FAIL'}) in {duration:.2f}s")

        assert exit_code in (0, None), (
            f"{name} exited with {exit_code}: wgsextract {' '.join(argv)}"
        )
        assert self.fake or duration >= min_duration, (
            f"{name} finished suspiciously fast ({duration:.2f}s < {min_duration}s)"
        )
        shutil.rmtree(test_dir, ignore_errors=True)

    def test_00_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code in (0, None)
        assert "usage" in capsys.readouterr().out.lower()

    # --- INFO ---
    def test_01_info(self) -> None:
        self.run_real("01 info", ["info", "--input", "{input}"], "ButtonBAMStats")

    def test_02_info_detailed(self) -> None:
        self.run_real(
            "02 info --detailed",
            ["info", "--detailed", "--input", "{input}", "--ref", "{ref}"],
            "ButtonBAMStats2",
        )

    # --- BAM ---
    def test_06_bam_sort_chrm(self) -> None:
        self.run_real(
            "06 bam sort (chrM)",
            ["bam", "sort", "--region", "chrM", "--input", "{input}", "--ref", "{ref}"],
            "GenSortedBAM",
            min_duration=300 if self.REGION is None else 0,
        )

    def test_07_bam_tocram_chrm(self) -> None:
        self.run_real(
            "07 bam to-cram (chrM)",
            [
                "bam",
                "to-cram",
                "--region",
                "chrM",
                "--input",
                "{input}",
                "--ref",
                "{ref}",
            ],
            "BAMtoCRAM",
            min_duration=60 if self.REGION is None else 0,
        )

    # --- EXTRACT ---
    def test_10_extract_mito(self) -> None:
        self.run_real(
            "10 extract mito",
            ["extract", "mito-fasta", "--input", "{input}", "--ref", "{ref}"],
            "ButtonMitoBAM",
        )

    def test_31_extract_mt_bam(self) -> None:
        self.run_real(
            "31 extract mt-bam",
            ["extract", "mt-bam", "--input", "{input}", "--ref", "{ref}"],
            "ButtonMitoBAM",
        )

    # --- VCF ---
    def test_13_vcf_snp_chrm(self) -> None:
        self.run_real(
            "13 vcf snp (chrM)",
            [
                "vcf",
                "snp",
                "--region",
                "chrM",
                "--input",
                "{input}",
                "--ref",
                "{ref}",
                "--ploidy",
                "1",
            ],
            "ButtonSNPVCF",
            min_duration=1800 if self.REGION is None else 0,
        )

    def test_32_vcf_freebayes_chrm(self) -> None:
        # FreeBayes lives in its own opt-in Pixi environment (`pixi install -e freebayes`).
        if not optional_tool_installed("freebayes"):
            pytest.skip("FreeBayes not found")
        self.run_real(
            "32 vcf freebayes (chrM)",
            ["vcf", "freebayes", "-r", "chrM", "--input", "{input}", "--ref", "{ref}"],
            "ButtonSNPVCF",
            min_duration=1800 if self.REGION is None else 0,
        )

    def test_37_vcf_gatk_chrm(self) -> None:
        if not optional_tool_installed("gatk"):
            pytest.skip("GATK not found")
        self.run_real(
            "37 vcf gatk (chrM)",
            ["vcf", "gatk", "-r", "chrM", "--input", "{input}", "--ref", "{ref}"],
            "ButtonSNPVCF",
            min_duration=3600 if self.REGION is None else 0,
        )

    def test_38_vcf_deepvariant_chrm(self) -> None:
        if shutil.which("run_deepvariant") is None:
            pytest.skip("run_deepvariant not found in PATH")
        self.run_real(
            "38 vcf deepvariant (chrM)",
            [
                "vcf",
                "deepvariant",
                "-r",
                "chrM",
                "--input",
                "{input}",
                "--ref",
                "{ref}",
            ],
            "ButtonSNPVCF",
            min_duration=3600 if self.REGION is None else 0,
        )

    def test_33_vcf_filter_gene(self) -> None:
        if not self.fake:
            pytest.skip("gene filter fixture VCF only exists for synthetic data")
        self.run_real(
            "33 vcf filter --gene BRCA1",
            [
                "vcf",
                "filter",
                "--gene",
                "BRCA1",
                "--input",
                "{input}",
                "--ref",
                "{ref}",
            ],
            "ButtonBAMStats",
            input_path=str(FAKE_DIR / "fake.vcf.gz"),
        )

    def test_34_vcf_trio_denovo(self, tmp_path: Path) -> None:
        header = (
            "##fileformat=VCFv4.2\n##contig=<ID=chrM,length=16569>\n"
            '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
        )
        record = "chrM\t100\t.\tA\tG\t100\tPASS\t.\tGT\t"
        vcfs = {}
        for member, gt in (("child", "0/1"), ("mom", "0/0"), ("dad", "0/0")):
            vcf = tmp_path / f"{member}.vcf"
            vcf.write_text(header + record + gt + "\n")
            run_command(["bgzip", str(vcf)])
            ensure_vcf_indexed(f"{vcf}.gz")
            vcfs[member] = f"{vcf}.gz"

        self.run_real(
            "34 vcf trio denovo",
            [
                "vcf",
                "trio",
                "--proband",
                "{input}",
                "--mother",
                vcfs["mom"],
                "--father",
                vcfs["dad"],
                "--mode",
                "denovo",
            ],
            "ButtonBAMStats",
            input_path=vcfs["child"],
        )

    def test_35_vcf_cnv_chrm(self) -> None:
        self.run_real(
            "35 vcf cnv (chrM)",
            [
                "vcf",
                "cnv",
                "--input",
                "{input}",
                "--ref",
                "{ref}",
                "--ploidy",
                "1",
            ],
            "ButtonBAMStats2",
        )

    def test_36_vep_chrm_offline(self, tmp_path: Path) -> None:
        if not (vep_cache_dir(self.ref) / "homo_sapiens").exists():
            pytest.skip("VEP cache not found, skipping offline E2E test")

        vcf = tmp_path / "test.vcf.gz"
        run_pipeline(
            [
                [
                    "bcftools",
                    "mpileup",
                    "-r",
                    "chrM:1-1000",
                    "-f",
                    self.ref,
                    self.input,
                    "-Ou",
                ],
                ["bcftools", "call", "-mv", "-Oz", "-o", str(vcf)],
            ],
            description="VEP test slice variant calling",
        )
        if vcf.stat().st_size < 100:
            pytest.skip("No variants in chrM:1-1000 for the VEP test slice")

        ensure_vcf_indexed(str(vcf))
        self.run_real(
            "36 vep offline (chrM)",
            ["vep", "--input", "{input}", "--ref", "{ref}", "--format", "vcf"],
            "ButtonBAMStats2",
            input_path=str(vcf),
        )
