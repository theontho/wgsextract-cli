"""Running a benchmark: the dataset, the core and heavy suites, the report."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wgsextract_cli.api._runtime import cancel_event, checkpoint
from wgsextract_cli.core.runtime import default_thread_tuning_profile
from wgsextract_cli.core.utils import UsageError, WGSExtractError

from .core import (
    _run_core_benchmark_steps,
)
from .environment import (
    _benchmark_external_tools,
    _format_progress_result,
    _normalize_region_for_fake_data,
    _normalize_region_for_ref,
    _prepare_real_benchmark_dataset,
    _print_external_tools,
    _print_failure_log_excerpt,
    _print_progress_header,
    _print_thread_policy,
)
from .heavy import (
    _run_heavy_processing_steps,
)
from .machine import (
    _machine_stats,
    _print_machine_stats,
    _record_base_file_size,
)
from .models import (
    EXCLUDED_OPERATIONS,
    PROFILE_DEFAULTS,
    REAL_BENCHMARK_DATASETS,
    BenchmarkJob,
    BenchmarkResult,
    BenchmarkThreadPlan,
    _say,
)
from .reports import (
    _write_report,
)

if TYPE_CHECKING:
    from wgsextract_cli.api.benchmark import BenchmarkParams


def _benchmark_root(params: BenchmarkParams) -> Path:
    if params.outdir:
        return params.outdir.expanduser().resolve()
    return (Path.cwd() / "out" / "benchmark").resolve()


def _benchmark_thread_plan(params: BenchmarkParams) -> BenchmarkThreadPlan:
    if params.threads is not None:
        return BenchmarkThreadPlan(
            str(params.threads), int(params.threads), "explicit --threads override"
        )

    tuning = default_thread_tuning_profile()
    return BenchmarkThreadPlan(tuning.label, tuning.threads, tuning.reason)


def run_benchmark(
    params: BenchmarkParams,
    *,
    debug: bool = False,
    quiet: bool = False,
    keep_awake: bool | None = None,
) -> None:
    """Run ``params``' benchmark; the CLI's ``debug``, ``quiet`` and
    ``keep_awake`` pass on to each step's child process."""
    profile = PROFILE_DEFAULTS[params.profile]
    coverage = params.coverage if params.coverage is not None else profile["coverage"]
    full_size = bool(params.full_size or profile["full_size"])
    region = params.region if params.region else profile["region"]
    target_count = (
        params.target_count
        if params.target_count is not None
        else profile["target_count"]
    )

    if coverage <= 0:
        raise UsageError("--coverage must be greater than zero.")
    if target_count <= 0:
        raise UsageError("--target-count must be greater than zero.")

    thread_plan = _benchmark_thread_plan(params)
    job = BenchmarkJob(
        threads=thread_plan.default_threads,
        memory=params.memory,
        debug=debug,
        quiet=quiet,
        keep_awake=keep_awake,
        seed=params.seed,
        dataset=params.dataset,
        dataset_zip=params.dataset_zip,
        dataset_url=params.dataset_url,
        dataset_sha256=params.dataset_sha256,
        dataset_cache_dir=params.dataset_cache_dir,
    )

    outdir = _benchmark_root(params)
    run_dir = outdir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        _run(
            params,
            job,
            thread_plan,
            outdir,
            run_dir,
            coverage,
            full_size,
            region,
            target_count,
        )
    except BaseException:
        token = cancel_event()
        if token is not None and token.cancelled:
            # A cancelled run's partial steps are of no use; cached datasets
            # live outside the run folder and stay.
            shutil.rmtree(run_dir, ignore_errors=True)
        raise


def _run(
    params: BenchmarkParams,
    job: BenchmarkJob,
    thread_plan: BenchmarkThreadPlan,
    outdir: Path,
    run_dir: Path,
    coverage: float,
    full_size: bool,
    region: str | None,
    target_count: int,
) -> None:
    dataset_dir = run_dir / "dataset"
    steps_dir = run_dir / "steps"
    logs_dir = run_dir / "logs"
    for path in (dataset_dir, steps_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    real_dataset = None
    ref_path = dataset_dir / "benchmark_ref.fa"
    generated_bam = dataset_dir / "fake.bam"
    target_tab_gz = dataset_dir / "benchmark_targets.tab.gz"
    benchmark_build = params.build
    data_source_description = "generated synthetic benchmark data"

    selected_dataset = params.dataset
    if selected_dataset in REAL_BENCHMARK_DATASETS:
        real_dataset = _prepare_real_benchmark_dataset(job, dataset_dir, outdir)
        ref_path = real_dataset.ref
        generated_bam = real_dataset.bam
        if real_dataset.targets:
            target_tab_gz = real_dataset.targets
        benchmark_build = real_dataset.build or benchmark_build
        data_source_description = real_dataset.description
        if not params.region and real_dataset.default_region:
            region = real_dataset.default_region
        if region and real_dataset.region_safe:
            region = _normalize_region_for_ref(region, ref_path)
    else:
        # The generated reference does not exist yet; match its naming instead
        # (e.g. the smoke profile's chrM is MT for hg19/hg37 fake data).
        region = _normalize_region_for_fake_data(region, benchmark_build)

    region_allowed = real_dataset is None or real_dataset.region_safe

    metadata: dict[str, Any] = {
        "profile": params.profile,
        "coverage": coverage,
        "full_size": full_size,
        "fake_bam_generator": "fast streaming reference-backed SNP generator",
        "data_source": selected_dataset,
        "data_source_description": data_source_description,
        "build": benchmark_build,
        "seed": params.seed,
        "region": region,
        "target_count": target_count,
        "suite": params.suite,
        "threads": thread_plan.label,
        "thread_policy": thread_plan.reason,
        "base_file": str(generated_bam),
        "base_file_size": None,
        "base_file_size_bytes": None,
        "external_tools": _benchmark_external_tools(),
        "machine_stats": _machine_stats(run_dir),
        "excluded_operations": EXCLUDED_OPERATIONS,
        "run_dir": str(run_dir),
    }

    results: list[BenchmarkResult] = []

    _print_machine_stats(metadata["machine_stats"])
    _print_external_tools(metadata["external_tools"])
    _print_thread_policy(thread_plan)
    _print_progress_header()

    def finish_report() -> None:
        _write_report(run_dir, metadata, results)

    def record(result: BenchmarkResult) -> BenchmarkResult:
        checkpoint()
        results.append(result)
        if result.slug in {"00-generate-bam", "00-load-real-dataset"}:
            _record_base_file_size(metadata, generated_bam)
            _say(
                f"Benchmark base file: {metadata['base_file']} "
                f"({metadata['base_file_size']})"
            )
        _say(_format_progress_result(result))
        if result.status == "FAIL" and not params.keep_going:
            _print_failure_log_excerpt(result)
            finish_report()
            detail = f"See {result.stderr_log}." if result.stderr_log else result.error
            raise WGSExtractError(f"Benchmark step failed: {result.name}. {detail}")
        return result

    analysis_bam, snp_vcf, unalign_r1, unalign_r2 = _run_core_benchmark_steps(
        job=job,
        record=record,
        real_dataset=real_dataset,
        dataset_dir=dataset_dir,
        steps_dir=steps_dir,
        logs_dir=logs_dir,
        generated_bam=generated_bam,
        ref_path=ref_path,
        target_tab_gz=target_tab_gz,
        benchmark_build=benchmark_build,
        coverage=coverage,
        full_size=full_size,
        target_count=target_count,
        region=region,
        region_allowed=region_allowed,
    )
    if params.suite == "heavy":
        _run_heavy_processing_steps(
            job=job,
            record=record,
            analysis_bam=analysis_bam,
            ref_path=ref_path,
            target_tab_gz=target_tab_gz,
            snp_vcf=snp_vcf,
            unalign_r1=unalign_r1,
            unalign_r2=unalign_r2,
            steps_dir=steps_dir,
            logs_dir=logs_dir,
            region=region,
        )

    finish_report()
    failed = [result.name for result in results if result.status == "FAIL"]
    if failed:
        raise WGSExtractError(
            f"{len(failed)} benchmark step(s) failed: " + "; ".join(failed)
        )
