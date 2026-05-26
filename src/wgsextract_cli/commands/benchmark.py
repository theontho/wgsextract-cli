import argparse
import os
from datetime import datetime

from wgsextract_cli.core.builds import (
    BUILD_CHOICES,
)
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.runtime import (
    RUNTIME_ENV_VAR,
    VALID_RUNTIME_MODES,
    get_tool_runtime_mode,
)
from wgsextract_cli.core.utils import WGSExtractError

from ._benchmark_core import (
    _run_core_benchmark_steps,
)
from ._benchmark_environment import (
    _benchmark_external_tools,
    _format_progress_result,
    _normalize_region_for_ref,
    _prepare_real_benchmark_dataset,
    _print_external_tools,
    _print_failure_log_excerpt,
    _print_progress_header,
    _print_thread_policy,
)
from ._benchmark_heavy import (
    _benchmark_root,
    _benchmark_thread_plan,
    _run_heavy_processing_steps,
)
from ._benchmark_machine import (
    _machine_stats,
    _print_machine_stats,
    _record_base_file_size,
)
from ._benchmark_models import (
    DEFAULT_REAL_DATASET_SHA256,
    DEFAULT_REAL_DATASET_URL,
    EXCLUDED_OPERATIONS,
    PROFILE_DEFAULTS,
    REAL_BENCHMARK_DATASETS,
    BenchmarkResult,
)
from ._benchmark_reports import (
    _write_report,
)


def run(args: argparse.Namespace) -> None:
    if getattr(args, "runtime", None):
        os.environ[RUNTIME_ENV_VAR] = str(args.runtime)

    profile = PROFILE_DEFAULTS[args.profile]
    coverage = args.coverage if args.coverage is not None else profile["coverage"]
    full_size = bool(args.full_size or profile["full_size"])
    region = args.region if args.region else profile["region"]
    target_count = (
        args.target_count if args.target_count is not None else profile["target_count"]
    )

    if coverage <= 0:
        raise WGSExtractError("--coverage must be greater than zero.")
    if target_count <= 0:
        raise WGSExtractError("--target-count must be greater than zero.")

    thread_plan = _benchmark_thread_plan(args)
    args._benchmark_thread_plan = thread_plan

    outdir = _benchmark_root(args)
    run_dir = outdir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dataset_dir = run_dir / "dataset"
    steps_dir = run_dir / "steps"
    logs_dir = run_dir / "logs"
    for path in (dataset_dir, steps_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    real_dataset = None
    ref_path = dataset_dir / "benchmark_ref.fa"
    generated_bam = dataset_dir / "fake.bam"
    target_tab_gz = dataset_dir / "benchmark_targets.tab.gz"
    benchmark_build = args.build
    data_source_description = "generated synthetic benchmark data"

    selected_dataset = getattr(args, "dataset", "fake")
    if selected_dataset in REAL_BENCHMARK_DATASETS:
        real_dataset = _prepare_real_benchmark_dataset(args, dataset_dir, outdir)
        ref_path = real_dataset.ref
        generated_bam = real_dataset.bam
        if real_dataset.targets:
            target_tab_gz = real_dataset.targets
        benchmark_build = real_dataset.build or benchmark_build
        data_source_description = real_dataset.description
        if not getattr(args, "region", None) and real_dataset.default_region:
            region = real_dataset.default_region
        if region and real_dataset.region_safe:
            region = _normalize_region_for_ref(region, ref_path)

    region_allowed = real_dataset is None or real_dataset.region_safe

    metadata = {
        "profile": args.profile,
        "coverage": coverage,
        "full_size": full_size,
        "fake_bam_generator": "fast streaming reference-backed SNP generator",
        "data_source": selected_dataset,
        "data_source_description": data_source_description,
        "build": benchmark_build,
        "seed": args.seed,
        "region": region,
        "target_count": target_count,
        "suite": args.suite,
        "tool_runtime": get_tool_runtime_mode(),
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
        results.append(result)
        if result.slug in {"00-generate-bam", "00-load-real-dataset"}:
            _record_base_file_size(metadata, generated_bam)
            print(
                f"Benchmark base file: {metadata['base_file']} "
                f"({metadata['base_file_size']})",
                flush=True,
            )
        print(_format_progress_result(result), flush=True)
        if result.status == "FAIL" and not args.keep_going:
            _print_failure_log_excerpt(result)
            finish_report()
            raise WGSExtractError(
                f"Benchmark step failed: {result.name}. See {result.stderr_log}."
            )
        return result

    analysis_bam, snp_vcf, unalign_r1, unalign_r2 = _run_core_benchmark_steps(
        args=args,
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
    if args.suite == "heavy":
        _run_heavy_processing_steps(
            args=args,
            record=record,
            analysis_bam=analysis_bam,
            generated_bam=generated_bam,
            ref_path=ref_path,
            target_tab_gz=target_tab_gz,
            snp_vcf=snp_vcf,
            unalign_r1=unalign_r1,
            unalign_r2=unalign_r2,
            steps_dir=steps_dir,
            logs_dir=logs_dir,
            build=benchmark_build,
            region=region,
        )

    finish_report()


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "benchmark",
        parents=[base_parser],
        help=CLI_HELP["cmd_benchmark"],
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_DEFAULTS),
        default="standard",
        help=(
            "Benchmark size preset. smoke is chrM-only, standard is a scaled "
            "human-like genome, 200mb targets a ~200 MiB fake BAM, and full "
            "uses real chromosome lengths."
        ),
    )
    parser.add_argument(
        "--coverage",
        type=float,
        help="Override generated BAM coverage for the selected profile.",
    )
    parser.add_argument(
        "--full-size",
        action="store_true",
        help="Use real chromosome lengths for the generated benchmark reference.",
    )
    parser.add_argument(
        "--build",
        choices=BUILD_CHOICES,
        default="hg38",
        help="Generated reference naming/build convention.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260504,
        help="Random seed for deterministic benchmark data generation.",
    )
    parser.add_argument(
        "--target-count",
        type=int,
        help="Approximate number of target SNPs for the microarray benchmark.",
    )
    parser.add_argument(
        "-r",
        "--region",
        help="Optional region to benchmark for region-aware operations.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue independent benchmark steps after a failed step when possible.",
    )
    parser.add_argument(
        "--suite",
        choices=["core", "heavy"],
        default="heavy",
        help="Benchmark operation coverage. core runs the lighter baseline suite.",
    )
    parser.add_argument(
        "--runtime",
        choices=sorted(VALID_RUNTIME_MODES),
        help="External tool runtime to benchmark: auto, native, wsl, cygwin, msys2, or pacman.",
    )
    parser.add_argument(
        "--dataset",
        choices=("fake", *sorted(REAL_BENCHMARK_DATASETS)),
        default="fake",
        help=(
            "Benchmark foundation data. fake generates synthetic data; real uses "
            "the release-backed mini genome zip; real-1x and real-30x cache "
            "public 1000 Genomes WGS references locally."
        ),
    )
    parser.add_argument(
        "--dataset-zip",
        help="Local real benchmark dataset zip. Overrides --dataset-url when --dataset real is used.",
    )
    parser.add_argument(
        "--dataset-url",
        default=DEFAULT_REAL_DATASET_URL,
        help="URL for the release-backed real benchmark dataset zip used by --dataset real.",
    )
    parser.add_argument(
        "--dataset-sha256",
        default=DEFAULT_REAL_DATASET_SHA256,
        help="Expected SHA-256 for --dataset-url downloads. Empty disables checksum validation.",
    )
    parser.add_argument(
        "--dataset-cache-dir",
        help="Directory for cached real benchmark dataset downloads.",
    )
    parser.set_defaults(func=run)
