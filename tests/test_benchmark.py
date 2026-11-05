import argparse
import hashlib
import json
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.fake_tools import python_tool, require_bare_name_fakes
from tests.htslib_helpers import needs_tools
from wgsextract_cli.commands import benchmark as _benchmark_entry
from wgsextract_cli.commands.benchmark import core as _benchmark_core
from wgsextract_cli.commands.benchmark import datasets as _benchmark_datasets
from wgsextract_cli.commands.benchmark import environment as _benchmark_environment
from wgsextract_cli.commands.benchmark import execution as _benchmark_execution
from wgsextract_cli.commands.benchmark import fixtures as _benchmark_fixtures
from wgsextract_cli.commands.benchmark import heavy as _benchmark_heavy
from wgsextract_cli.commands.benchmark import machine as _benchmark_machine
from wgsextract_cli.commands.benchmark import models as _benchmark_models
from wgsextract_cli.commands.benchmark import qc as _benchmark_qc
from wgsextract_cli.commands.benchmark import reports as _benchmark_reports
from wgsextract_cli.commands.benchmark import setup as _benchmark_setup
from wgsextract_cli.core.runtime import default_thread_tuning_profile


class _ModuleGroup:
    def __init__(self, *modules):
        object.__setattr__(self, "_modules", modules)

    def __getattr__(self, name):
        for module in self._modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        for module in self._modules:
            if hasattr(module, name):
                setattr(module, name, value)
        object.__setattr__(self, name, value)


benchmark = _ModuleGroup(
    _benchmark_entry,
    _benchmark_models,
    _benchmark_execution,
    _benchmark_qc,
    _benchmark_fixtures,
    _benchmark_heavy,
    _benchmark_datasets,
    _benchmark_environment,
    _benchmark_machine,
    _benchmark_reports,
    _benchmark_setup,
    _benchmark_core,
)


def test_benchmark_suite_defaults_to_heavy() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    benchmark.register(subparsers, argparse.ArgumentParser(add_help=False))

    args = parser.parse_args(["benchmark"])

    assert args.suite == "heavy"


def test_benchmark_accepts_build_aliases() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    benchmark.register(subparsers, argparse.ArgumentParser(add_help=False))

    assert parser.parse_args(["benchmark", "--build", "GRCh37"]).build == "GRCh37"
    assert parser.parse_args(["benchmark", "--build", "hs38d1"]).build == "hs38d1"


def test_benchmark_accepts_real_coverage_dataset_tags() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    benchmark.register(subparsers, argparse.ArgumentParser(add_help=False))

    assert parser.parse_args(["benchmark", "--dataset", "real-1x"]).dataset == "real-1x"
    assert (
        parser.parse_args(["benchmark", "--dataset", "real-30x"]).dataset == "real-30x"
    )


@pytest.mark.parametrize(
    ("build", "expected_ploidy"),
    [
        ("hg19", "GRCh37"),
        ("hg37", "GRCh37"),
        ("GRCh37", "GRCh37"),
        ("hs37d5", "GRCh37"),
        ("hg38", "GRCh38"),
        ("GRCh38", "GRCh38"),
        ("hs38DH", "GRCh38"),
        ("hs38d1", "GRCh38"),
        ("t2t", "GRCh38"),
    ],
)
def test_benchmark_ploidy_for_build_aliases(build: str, expected_ploidy: str) -> None:
    assert benchmark.ploidy_for_build(build) == expected_ploidy


def test_benchmark_download_rejects_non_http_url(tmp_path: Path, monkeypatch) -> None:
    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("urlopen should not be called for non-HTTP URLs")

    from wgsextract_cli.core.ref_library import downloads

    monkeypatch.setattr(downloads, "urlopen", fail_urlopen)

    with pytest.raises(ValueError, match="Unsupported benchmark dataset URL scheme"):
        benchmark._download_file("file:///tmp/dataset.zip", tmp_path / "dataset.zip")


def test_benchmark_download_error_redacts_url_query(
    tmp_path: Path, monkeypatch
) -> None:
    def fail_urlopen(*_args, **_kwargs):
        raise OSError("network unavailable")

    from wgsextract_cli.core.ref_library import downloads

    monkeypatch.setattr(downloads, "urlopen", fail_urlopen)

    with pytest.raises(benchmark.WGSExtractError) as excinfo:
        benchmark._download_file(
            "https://example.test/dataset.zip?token=secret",
            tmp_path / "dataset.zip",
        )

    message = str(excinfo.value)
    assert "dataset.zip" in message
    assert "token=secret" not in message


def test_benchmark_result_names_include_cli_command_labels() -> None:
    assert (
        benchmark._name_with_command_label(
            "Reference N-base counting", ["ref", "count-ns", "--ref", "ref.fa"]
        )
        == "Reference N-base counting [ref count-ns]"
    )
    assert (
        benchmark._name_with_command_label(
            "Microarray CombinedKit generation", ["microarray", "--input", "sample.bam"]
        )
        == "Microarray CombinedKit generation [microarray]"
    )
    assert (
        benchmark._name_with_command_label(
            "BAM metadata and sequencing metrics",
            ["info", "--detailed", "--input", "sample.bam"],
            "info --detailed",
        )
        == "BAM metadata and sequencing metrics [info --detailed]"
    )


def test_cpu_frequency_handles_psutil_exception(monkeypatch) -> None:
    def raise_cpu_freq_error():
        raise RuntimeError("cpu frequency unavailable")

    monkeypatch.setattr(
        benchmark.psutil, "cpu_freq", raise_cpu_freq_error, raising=False
    )

    assert benchmark._cpu_frequency() is None


needs_benchmark_tools = needs_tools("samtools", "bcftools", "bgzip", "tabix", "bwa")


@dataclass
class BenchmarkRun:
    stdout: str
    run_dir: Path

    @property
    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(
            (self.run_dir / "benchmark_results.json").read_text()
        )
        return document

    @property
    def results(self) -> dict[str, dict[str, Any]]:
        return {result["slug"]: result for result in self.document["results"]}


def _benchmark(outdir: Path, *argv: str) -> BenchmarkRun:
    """A real smoke benchmark on generated data, as the user runs it."""
    completed = subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "benchmark", "--profile",
         "smoke", "--dataset", "fake", "--outdir", str(outdir), *argv],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stdout + completed.stderr
    (run_dir,) = (outdir / "runs").iterdir()
    return BenchmarkRun(completed.stdout, run_dir)


@pytest.fixture(scope="module")
def core_run(tmp_path_factory: pytest.TempPathFactory) -> BenchmarkRun:
    return _benchmark(tmp_path_factory.mktemp("core"), "--suite", "core")


@pytest.fixture(scope="module")
def heavy_run(tmp_path_factory: pytest.TempPathFactory) -> BenchmarkRun:
    return _benchmark(
        tmp_path_factory.mktemp("heavy"), "--threads", "1", "--region", "chrM:1-9000"
    )


@needs_benchmark_tools
def test_benchmark_prints_machine_progress_and_base_file_size(
    core_run: BenchmarkRun,
) -> None:
    stdout = core_run.stdout
    assert f"Cores: {psutil.cpu_count(logical=False)} physical / " in stdout
    samtools = next(
        tool
        for tool in core_run.document["metadata"]["external_tools"]
        if tool["name"] == "samtools"
    )
    assert samtools["status"] == "active"
    assert f"{samtools['runtime']}: {samtools['path']}" in stdout
    # The thread policy the user didn't override: this machine's default.
    tuning = default_thread_tuning_profile()
    assert f"Threads: {tuning.label}" in stdout
    assert f"Policy: {tuning.reason}" in stdout

    base = core_run.run_dir / "dataset" / "fake.bam"
    (base_line,) = [
        line for line in stdout.splitlines() if "Benchmark base file:" in line
    ]
    assert str(base) in base_line
    assert f"({base.stat().st_size:,} bytes)" in base_line

    # One progress line per step, each PASS, then the summary.
    results = core_run.document["results"]
    assert results and {r["status"] for r in results} == {"PASS"}
    for result in results:
        assert stdout.count(result["name"]) == 1
    assert f"Passed: {len(results)} | Failed: 0 | Skipped: 0" in stdout
    assert "Suite: core" in stdout


@needs_benchmark_tools
def test_heavy_suite_adds_processing_steps_on_the_aligned_bam_and_region(
    core_run: BenchmarkRun, heavy_run: BenchmarkRun
) -> None:
    heavy = heavy_run.results
    assert "Suite: heavy" in heavy_run.stdout
    assert "Policy: explicit --threads override" in heavy_run.stdout
    assert {r["status"] for r in heavy.values()} <= {"PASS", "SKIP"}
    # The core suite's steps, then the heavy ones, among them the commands that
    # only run locally.
    extra = heavy.keys() - core_run.results.keys()
    assert core_run.results.keys() < heavy.keys()
    assert {"12a-ref-count-ns", "12b-ref-verify", "17a-analyze-batch-fixture",
            "17b-analyze-batch-gen", "18a-repair-fixtures", "18b-repair-ftdna-bam",
            "18c-repair-ftdna-vcf"} <= extra  # fmt: skip
    step_numbers = [r["slug"].split("-", 1)[0] for r in heavy_run.document["results"]]
    assert len(step_numbers) == len(set(step_numbers))

    # Heavy steps read the aligned BAM and SNP calls, over the chosen region.
    aligned = heavy["04-fastq-align"]["expected_outputs"][0]
    assert Path(aligned).name == "benchmark_aligned.bam"
    assert aligned in heavy["14f-custom-extract"]["command"]
    assert "chrM:1-9000" in heavy["14f-custom-extract"]["command"]
    snps = next(
        p for p in heavy["08-vcf-snp"]["expected_outputs"] if p.endswith(".vcf.gz")
    )
    assert snps in heavy["16a-vcf-filter"]["command"]
    # contract: --threads reaches every step.
    for result in heavy.values():
        if result["command"] and "wgsextract_cli.main" in result["command"]:
            assert result["command"][-2:] == ["--threads", "1"], result["slug"]


def test_200mb_profile_targets_scaled_fake_bam() -> None:
    profile = benchmark.PROFILE_DEFAULTS["200mb"]

    assert profile["coverage"] == 71.0
    assert profile["full_size"] is False
    assert profile["region"] is None


def test_command_region_expands_whole_contig_region(tmp_path: Path) -> None:
    ref = tmp_path / "mini.fa"
    Path(str(ref) + ".fai").write_text("20\t1000\t4\t80\t81\n", encoding="utf-8")

    assert benchmark._command_region("20", ref) == "20:1-1000"
    assert benchmark._command_region("20:10-20", ref) == "20:10-20"
    assert benchmark._command_region(None, ref) is None


def test_target_ranges_match_mito_aliases() -> None:
    assert benchmark._target_ranges([("chrM", 16569)], "MT:10-20") == [("chrM", 10, 20)]
    assert benchmark._target_ranges([("MT", 16569)], "chrM") == [("MT", 1, 16569)]


def test_normalize_region_for_ref_matches_chr_prefix(tmp_path: Path) -> None:
    ref = tmp_path / "grch38.fa"
    Path(str(ref) + ".fai").write_text(
        "chr20\t1000\t6\t80\t81\nchrM\t16569\t1020\t80\t81\n", encoding="utf-8"
    )

    assert benchmark._normalize_region_for_ref("20", ref) == "chr20"
    assert benchmark._normalize_region_for_ref("20:10-20", ref) == "chr20:10-20"
    assert benchmark._normalize_region_for_ref("MT", ref) == "chrM"


def test_direct_real_dataset_manifest_marks_region_safe(tmp_path: Path) -> None:
    spec = benchmark.REAL_BENCHMARK_DATASETS["real-30x"]
    ref = tmp_path / "ref.fa"
    bam = tmp_path / "sample.cram"
    bai = tmp_path / "sample.cram.crai"
    ref.write_text(">chrM\nACGT\n", encoding="utf-8")
    bam.write_bytes(b"cram")
    bai.write_bytes(b"index")

    benchmark._write_direct_dataset_manifest(
        spec,
        tmp_path,
        {"ref": ref, "bam": bam, "bam_index": bai},
    )

    dataset = benchmark._load_real_benchmark_dataset(tmp_path)

    assert dataset.dataset_id == spec.dataset_id
    assert dataset.region_safe is True
    assert dataset.bam == bam


@pytest.mark.parametrize(
    ("build", "region", "expected"),
    [
        ("hg19", "chrM", "MT"),
        ("hg37", "chr20:1-100", "20:1-100"),
        ("hg38", "MT", "chrM"),
        ("t2t", "20", "chr20"),
        ("hg38", None, None),
    ],
)
def test_normalize_region_for_fake_data(build, region, expected) -> None:
    assert benchmark._normalize_region_for_fake_data(region, build) == expected


def test_windows_ram_speed_comes_from_powershell_cim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_bare_name_fakes()
    log = tmp_path / "powershell.json"
    python_tool(tmp_path, "powershell", f"""
import json, sys
open({str(log)!r}, "w").write(json.dumps(sys.argv[1:]))
print("3200\\n3200\\n\\n2667")
""")  # fmt: skip
    python_tool(tmp_path, "wmic", "raise SystemExit('wmic is gone')")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")

    assert benchmark._ram_speed() == "3200, 2667 MT/s"
    # contract: wmic is removed from recent Windows 11; CIM replaces it.
    assert "Get-CimInstance Win32_PhysicalMemory" in json.loads(log.read_text())[-1]


def test_dataset_zip_sha256_is_verified(tmp_path: Path) -> None:
    zip_path = tmp_path / "dataset.zip"
    zip_path.write_bytes(b"zip")
    digest = hashlib.sha256(b"zip").hexdigest()
    args = Namespace(dataset_zip=str(zip_path), dataset_sha256=digest)

    assert benchmark._real_dataset_zip_path(args, tmp_path) == zip_path.resolve()
    args.dataset_sha256 = "0" * 64
    with pytest.raises(benchmark.WGSExtractError, match="SHA256 mismatch"):
        benchmark._real_dataset_zip_path(args, tmp_path)
