import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from wgsextract_cli.core.dependencies import (
    get_tool_path,
    get_tool_runtime,
    get_tool_version,
)
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.runtime import (
    RUNTIME_ENV_VAR,
    VALID_RUNTIME_MODES,
    default_thread_tuning_profile,
    get_tool_runtime_mode,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "smoke": {
        "coverage": 0.1,
        "full_size": False,
        "region": "chrM",
        "target_count": 250,
    },
    "standard": {
        "coverage": 1.0,
        "full_size": False,
        "region": None,
        "target_count": 10_000,
    },
    "200mb": {
        # The scaled reference produces ~104 MiB at 37x on current fake data.
        "coverage": 71.0,
        "full_size": False,
        "region": None,
        "target_count": 100_000,
    },
    "full": {
        "coverage": 1.0,
        "full_size": True,
        "region": None,
        "target_count": 250_000,
    },
}

EXCLUDED_OPERATIONS = "VEP, DeepVariant, Yleaf, Haplogrep"
PROGRESS_STEP_WIDTH = 68
DEFAULT_REAL_DATASET_URL = (
    "https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/"
    "wgsextract-benchmark-hg19-mini.zip"
)
DEFAULT_REAL_DATASET_SHA256 = (
    "ad0f8070dc5ca35c4a6de540493a81df082d160417f747ae68d9c098c110a9f6"
)


@dataclass(frozen=True)
class BenchmarkToolSpec:
    name: str
    required: bool
    purpose: str


BENCHMARK_EXTERNAL_TOOLS: tuple[BenchmarkToolSpec, ...] = (
    BenchmarkToolSpec("samtools", True, "BAM/CRAM/FASTA indexing and conversion"),
    BenchmarkToolSpec("bcftools", True, "SNP/indel calling and VCF statistics"),
    BenchmarkToolSpec("bgzip", True, "Target and VCF compression"),
    BenchmarkToolSpec("tabix", True, "Compressed target and VCF indexing"),
    BenchmarkToolSpec("bwa", True, "Short-read alignment"),
    BenchmarkToolSpec(
        "sambamba",
        False,
        "BAM sort/index/view acceleration when available on non-macOS",
    ),
    BenchmarkToolSpec(
        "samblaster",
        False,
        "Duplicate-marking stage during alignment when available",
    ),
    BenchmarkToolSpec("fastp", False, "FASTQ read trimming and QC"),
    BenchmarkToolSpec("fastqc", False, "FASTQ quality-control reports"),
    BenchmarkToolSpec("freebayes", False, "Haplotype-based variant calling"),
)


@dataclass
class BenchmarkResult:
    name: str
    slug: str
    status: str
    seconds: float
    command: list[str]
    output_dir: str
    stdout_log: str | None = None
    stderr_log: str | None = None
    returncode: int | None = None
    expected_outputs: list[str] | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class BenchmarkThreadPlan:
    label: str
    default_threads: int | None
    per_step_threads: dict[str, int]
    reason: str


@dataclass(frozen=True)
class BenchmarkDataset:
    dataset_id: str
    description: str
    build: str
    root: Path
    ref: Path
    bam: Path
    bam_index: Path | None
    cram: Path | None
    cram_index: Path | None
    fastq_r1: Path | None
    fastq_r2: Path | None
    vcf: Path | None
    vcf_index: Path | None
    targets: Path | None
    targets_index: Path | None
    default_region: str | None
    manifest: dict[str, Any]


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
):
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
        choices=["hg38", "hg19", "hg37", "t2t"],
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
        choices=("fake", "real"),
        default="fake",
        help="Benchmark foundation data. fake generates synthetic data; real uses a release-backed mini genome zip.",
    )
    parser.add_argument(
        "--dataset-zip",
        help="Local real benchmark dataset zip. Overrides --dataset-url when --dataset real is used.",
    )
    parser.add_argument(
        "--dataset-url",
        default=DEFAULT_REAL_DATASET_URL,
        help="URL for the release-backed real benchmark dataset zip.",
    )
    parser.add_argument(
        "--dataset-sha256",
        default=DEFAULT_REAL_DATASET_SHA256,
        help="Expected SHA-256 for --dataset-url downloads. Empty disables checksum validation.",
    )
    parser.add_argument(
        "--dataset-cache-dir",
        help="Directory for cached real benchmark dataset zip downloads.",
    )
    parser.set_defaults(func=run)


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
    if selected_dataset == "real":
        real_dataset = _prepare_real_benchmark_dataset(args, dataset_dir, outdir)
        ref_path = real_dataset.ref
        generated_bam = real_dataset.bam
        if real_dataset.targets:
            target_tab_gz = real_dataset.targets
        benchmark_build = real_dataset.build or benchmark_build
        data_source_description = real_dataset.description
        if not getattr(args, "region", None) and real_dataset.default_region:
            region = real_dataset.default_region

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

    if real_dataset:
        record(
            _run_internal_step(
                name="Load release-backed real genome dataset",
                slug="00-load-real-dataset",
                output_dir=dataset_dir,
                func=lambda: None,
                expected_outputs=_existing_dataset_outputs(real_dataset),
            )
        )
    else:
        record(
            _run_cli_step(
                args,
                name="Generate deterministic BAM foundation",
                slug="00-generate-bam",
                command_args=[
                    "qc",
                    "fake-data",
                    "--outdir",
                    str(dataset_dir),
                    "--ref",
                    str(ref_path),
                    "--build",
                    benchmark_build,
                    "--coverage",
                    str(coverage),
                    "--type",
                    "bam",
                    "--seed",
                    str(args.seed),
                ]
                + (["--full-size"] if full_size else []),
                output_dir=dataset_dir,
                logs_dir=logs_dir,
                expected_outputs=[
                    generated_bam,
                    Path(str(generated_bam) + ".bai"),
                    ref_path,
                ],
            )
        )

    record(
        _run_cli_step(
            args,
            name="BAM metadata and sequencing metrics",
            slug="00a-info-detailed",
            command_args=[
                "info",
                "--detailed",
                "--input",
                str(generated_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(steps_dir / "info"),
            ],
            output_dir=steps_dir / "info",
            logs_dir=logs_dir,
            expected_outputs=[],
            command_label="info --detailed",
        )
    )

    record(
        _run_cli_step(
            args,
            name="Index generated reference for alignment",
            slug="01-reference-index",
            command_args=["ref", "index", "--ref", str(ref_path)],
            output_dir=dataset_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                Path(str(ref_path) + ".bwt"),
                Path(str(ref_path) + ".fai"),
            ],
        )
    )

    if real_dataset and real_dataset.targets:
        expected_targets = [real_dataset.targets]
        if real_dataset.targets_index:
            expected_targets.append(real_dataset.targets_index)
        record(
            _run_internal_step(
                name="Load real microarray targets",
                slug="02-microarray-targets",
                output_dir=dataset_dir,
                func=lambda: None,
                expected_outputs=expected_targets,
            )
        )
    else:
        record(
            _run_internal_step(
                name="Generate deterministic microarray targets",
                slug="02-microarray-targets",
                output_dir=dataset_dir,
                func=lambda: _create_target_snp_tab(
                    ref_path, target_tab_gz, target_count, region
                ),
                expected_outputs=[target_tab_gz, Path(str(target_tab_gz) + ".tbi")],
            )
        )

    unalign_dir = steps_dir / "bam-unalign"
    unalign_r1 = unalign_dir / "benchmark_R1.fastq.gz"
    unalign_r2 = unalign_dir / "benchmark_R2.fastq.gz"
    unalign_cmd = [
        "bam",
        "unalign",
        "--input",
        str(generated_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(unalign_dir),
        "--r1",
        unalign_r1.name,
        "--r2",
        unalign_r2.name,
    ]
    if region and not real_dataset:
        unalign_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="BAM unalignment to paired FASTQ",
            slug="03-bam-unalign",
            command_args=unalign_cmd,
            output_dir=unalign_dir,
            logs_dir=logs_dir,
            expected_outputs=[unalign_r1, unalign_r2],
        )
    )

    align_dir = steps_dir / "fastq-align"
    align_r1 = (
        real_dataset.fastq_r1 if real_dataset and real_dataset.fastq_r1 else unalign_r1
    )
    align_r2 = (
        real_dataset.fastq_r2 if real_dataset and real_dataset.fastq_r2 else unalign_r2
    )
    aligned_bam = align_dir / f"{_align_output_stem(align_r1)}_aligned.bam"
    record(
        _run_cli_step(
            args,
            name="FASTQ alignment to BAM",
            slug="04-fastq-align",
            command_args=[
                "align",
                "--r1",
                str(align_r1),
                "--r2",
                str(align_r2),
                "--ref",
                str(ref_path),
                "--outdir",
                str(align_dir),
                "--format",
                "BAM",
            ],
            output_dir=align_dir,
            logs_dir=logs_dir,
            expected_outputs=[aligned_bam, Path(str(aligned_bam) + ".bai")],
        )
    )

    analysis_bam = aligned_bam if aligned_bam.exists() else generated_bam

    subset_dir = steps_dir / "bam-subset"
    subset_bam = subset_dir / f"{analysis_bam.stem}_subset.bam"
    subset_cmd = [
        "extract",
        "bam-subset",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(subset_dir),
        "--fraction",
        "0.1",
    ]
    if region and not real_dataset:
        subset_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="BAM subset extraction",
            slug="04a-bam-subset",
            command_args=subset_cmd,
            output_dir=subset_dir,
            logs_dir=logs_dir,
            expected_outputs=[subset_bam],
        )
    )

    mt_bam_dir = steps_dir / "mt-bam"
    mt_bam = mt_bam_dir / f"{analysis_bam.stem}_mtDNA.bam"
    record(
        _run_cli_step(
            args,
            name="Mitochondrial BAM extraction",
            slug="04b-mt-bam",
            command_args=[
                "extract",
                "mt-bam",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(mt_bam_dir),
            ],
            output_dir=mt_bam_dir,
            logs_dir=logs_dir,
            expected_outputs=[mt_bam, Path(str(mt_bam) + ".bai")],
        )
    )

    ydna_bam_dir = steps_dir / "ydna-bam"
    ydna_bam = ydna_bam_dir / f"{analysis_bam.stem}_Y.bam"
    record(
        _run_cli_step(
            args,
            name="Y-DNA BAM extraction",
            slug="04c-ydna-bam",
            command_args=[
                "extract",
                "ydna-bam",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(ydna_bam_dir),
            ],
            output_dir=ydna_bam_dir,
            logs_dir=logs_dir,
            expected_outputs=[ydna_bam, Path(str(ydna_bam) + ".bai")],
        )
    )

    sort_dir = steps_dir / "bam-sort"
    sorted_bam = sort_dir / f"{analysis_bam.stem}_sorted.bam"
    record(
        _run_cli_step(
            args,
            name="BAM coordinate sort",
            slug="05-bam-sort",
            command_args=[
                "bam",
                "sort",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(sort_dir),
            ],
            output_dir=sort_dir,
            logs_dir=logs_dir,
            expected_outputs=[sorted_bam],
        )
    )

    cram_dir = steps_dir / "bam-to-cram"
    cram_path = cram_dir / f"{analysis_bam.stem}.cram"
    to_cram = record(
        _run_cli_step(
            args,
            name="BAM to CRAM conversion",
            slug="06-bam-to-cram",
            command_args=[
                "bam",
                "to-cram",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(cram_dir),
            ]
            + (["--cram-version", "2.1"] if real_dataset else []),
            output_dir=cram_dir,
            logs_dir=logs_dir,
            expected_outputs=[cram_path, Path(str(cram_path) + ".crai")],
        )
    )

    bam_roundtrip_dir = steps_dir / "cram-to-bam"
    if to_cram.success:
        record(
            _run_cli_step(
                args,
                name="CRAM to BAM conversion",
                slug="07-cram-to-bam",
                command_args=[
                    "bam",
                    "to-bam",
                    "--input",
                    str(cram_path),
                    "--ref",
                    str(ref_path),
                    "--outdir",
                    str(bam_roundtrip_dir),
                ],
                output_dir=bam_roundtrip_dir,
                logs_dir=logs_dir,
                expected_outputs=[bam_roundtrip_dir / f"{cram_path.stem}.bam"],
            )
        )
    else:
        record(
            _skipped_result(
                "CRAM to BAM conversion",
                "07-cram-to-bam",
                bam_roundtrip_dir,
                "BAM to CRAM conversion failed.",
            )
        )

    snp_dir = steps_dir / "vcf-snp"
    snp_vcf = snp_dir / "snps.vcf.gz"
    snp_cmd = [
        "vcf",
        "snp",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(snp_dir),
        "--ploidy",
        _ploidy_for_build(benchmark_build),
    ]
    if region and not real_dataset:
        snp_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="VCF SNP generation",
            slug="08-vcf-snp",
            command_args=snp_cmd,
            output_dir=snp_dir,
            logs_dir=logs_dir,
            expected_outputs=[snp_vcf, Path(str(snp_vcf) + ".tbi")],
        )
    )

    indel_dir = steps_dir / "vcf-indel"
    indel_cmd = [
        "vcf",
        "indel",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(indel_dir),
        "--ploidy",
        _ploidy_for_build(benchmark_build),
    ]
    if region and not real_dataset:
        indel_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="VCF indel generation",
            slug="09-vcf-indel",
            command_args=indel_cmd,
            output_dir=indel_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                indel_dir / "indels.vcf.gz",
                indel_dir / "indels.vcf.gz.tbi",
            ],
        )
    )

    microarray_dir = steps_dir / "microarray"
    combined_kit = microarray_dir / f"{analysis_bam.stem}_CombinedKit.txt"
    microarray_cmd = [
        "microarray",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(microarray_dir),
        "--ref-vcf-tab",
        str(target_tab_gz),
        "--formats",
        "all",
    ]
    if region and not real_dataset:
        microarray_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="Microarray CombinedKit generation",
            slug="10-microarray",
            command_args=microarray_cmd,
            output_dir=microarray_dir,
            logs_dir=logs_dir,
            expected_outputs=[combined_kit],
        )
    )

    qc_dir = steps_dir / "vcf-qc"
    qc_vcf = (
        real_dataset.vcf
        if real_dataset and real_dataset.vcf
        else snp_dir / "snps.vcf.gz"
    )
    record(
        _run_cli_step(
            args,
            name="VCF quality-control statistics",
            slug="11-vcf-qc",
            command_args=[
                "qc",
                "vcf",
                "--input",
                str(qc_vcf),
                "--outdir",
                str(qc_dir),
            ],
            output_dir=qc_dir,
            logs_dir=logs_dir,
            expected_outputs=[qc_dir / f"{qc_vcf.name}.vcfstats.txt"],
        )
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


def _run_heavy_processing_steps(
    *,
    args: argparse.Namespace,
    record: Any,
    analysis_bam: Path,
    generated_bam: Path,
    ref_path: Path,
    target_tab_gz: Path,
    snp_vcf: Path,
    unalign_r1: Path,
    unalign_r2: Path,
    steps_dir: Path,
    logs_dir: Path,
    build: str,
    region: str | None,
) -> None:
    del generated_bam, build
    base_name = analysis_bam.name.split(".")[0]
    heavy_region = region or _default_heavy_region(ref_path)
    command_region = _command_region(region, ref_path)
    command_chrom = _chrom_only_region(command_region)

    ref_count_dir = steps_dir / "ref-count-ns"
    record(
        _run_cli_step(
            args,
            name="Reference N-base counting",
            slug="12a-ref-count-ns",
            command_args=[
                "ref",
                "count-ns",
                "--ref",
                str(ref_path),
                "--outdir",
                str(ref_count_dir),
            ],
            output_dir=ref_count_dir,
            logs_dir=logs_dir,
            expected_outputs=[],
        )
    )

    ref_verify_dir = steps_dir / "ref-verify"
    record(
        _run_cli_step(
            args,
            name="Reference integrity verification",
            slug="12b-ref-verify",
            command_args=[
                "ref",
                "verify",
                "--ref",
                str(ref_path),
                "--outdir",
                str(ref_verify_dir),
            ],
            output_dir=ref_verify_dir,
            logs_dir=logs_dir,
            expected_outputs=[Path(str(ref_path) + ".fai")],
        )
    )

    identify_dir = steps_dir / "bam-identify"
    record(
        _run_cli_step(
            args,
            name="BAM reference identification",
            slug="12a-bam-identify",
            command_args=[
                "bam",
                "identify",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(identify_dir),
            ],
            output_dir=identify_dir,
            logs_dir=logs_dir,
            expected_outputs=[],
        )
    )

    index_dir = steps_dir / "bam-index-cycle"
    index_fixture = index_dir / "benchmark_index_fixture.bam"
    record(
        _run_internal_step(
            name="Prepare BAM index benchmark fixture",
            slug="12b-bam-index-fixture",
            output_dir=index_dir,
            func=lambda: _copy_bam_with_index(analysis_bam, index_fixture),
            expected_outputs=[index_fixture, Path(str(index_fixture) + ".bai")],
        )
    )
    record(
        _run_cli_step(
            args,
            name="BAM index removal",
            slug="12c-bam-unindex",
            command_args=[
                "bam",
                "unindex",
                "--input",
                str(index_fixture),
                "--outdir",
                str(index_dir),
            ],
            output_dir=index_dir,
            logs_dir=logs_dir,
            expected_outputs=[index_fixture],
        )
    )
    record(
        _run_internal_step(
            name="Verify BAM index removal",
            slug="12d-bam-unindex-verify",
            output_dir=index_dir,
            func=lambda: _assert_bam_unindexed(index_fixture),
            expected_outputs=[index_fixture],
        )
    )
    record(
        _run_cli_step(
            args,
            name="BAM index creation",
            slug="12e-bam-index",
            command_args=[
                "bam",
                "index",
                "--input",
                str(index_fixture),
                "--outdir",
                str(index_dir),
            ],
            output_dir=index_dir,
            logs_dir=logs_dir,
            expected_outputs=[Path(str(index_fixture) + ".bai")],
        )
    )

    unsort_dir = steps_dir / "bam-unsort"
    record(
        _run_cli_step(
            args,
            name="BAM header unsort conversion",
            slug="12f-bam-unsort",
            command_args=[
                "bam",
                "unsort",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(unsort_dir),
            ],
            output_dir=unsort_dir,
            logs_dir=logs_dir,
            expected_outputs=[unsort_dir / f"{analysis_bam.stem}_unsorted.bam"],
        )
    )

    full_coverage_dir = steps_dir / "coverage-full"
    full_coverage_cmd = [
        "info",
        "calculate-coverage",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(full_coverage_dir),
    ]
    if command_region:
        full_coverage_cmd += ["--region", command_region]
    record(
        _run_cli_step(
            args,
            name="BAM full coverage calculation",
            slug="13a-info-calculate-coverage",
            command_args=full_coverage_cmd,
            output_dir=full_coverage_dir,
            logs_dir=logs_dir,
            expected_outputs=[full_coverage_dir / f"{analysis_bam.name}_bincvg.csv"],
        )
    )

    sampled_coverage_dir = steps_dir / "coverage-sample"
    sampled_coverage_cmd = [
        "info",
        "coverage-sample",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(sampled_coverage_dir),
    ]
    if command_chrom:
        sampled_coverage_cmd += ["--region", command_chrom]
    record(
        _run_cli_step(
            args,
            name="BAM sampled coverage calculation",
            slug="13b-info-coverage-sample",
            command_args=sampled_coverage_cmd,
            output_dir=sampled_coverage_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                sampled_coverage_dir / f"{analysis_bam.name}_samplecvg.json"
            ],
        )
    )

    mito_fasta_dir = steps_dir / "mito-fasta"
    record(
        _run_cli_step(
            args,
            name="Mitochondrial FASTA consensus",
            slug="14a-mito-fasta",
            command_args=[
                "extract",
                "mito-fasta",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(mito_fasta_dir),
            ],
            output_dir=mito_fasta_dir,
            logs_dir=logs_dir,
            expected_outputs=[mito_fasta_dir / f"{base_name}_MT.fasta"],
        )
    )

    mito_vcf_dir = steps_dir / "mito-vcf"
    record(
        _run_cli_step(
            args,
            name="Mitochondrial VCF extraction",
            slug="14b-mito-vcf",
            command_args=[
                "extract",
                "mito-vcf",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(mito_vcf_dir),
            ],
            output_dir=mito_vcf_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                mito_vcf_dir / f"{base_name}_MT.vcf.gz",
                mito_vcf_dir / f"{base_name}_MT.vcf.gz.tbi",
            ],
        )
    )

    ydna_vcf_dir = steps_dir / "ydna-vcf"
    record(
        _run_cli_step(
            args,
            name="Y-DNA VCF extraction",
            slug="14c-ydna-vcf",
            command_args=[
                "extract",
                "ydna-vcf",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(ydna_vcf_dir),
            ],
            output_dir=ydna_vcf_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                ydna_vcf_dir / f"{base_name}_Y.vcf.gz",
                ydna_vcf_dir / f"{base_name}_Y.vcf.gz.tbi",
            ],
        )
    )

    y_mt_dir = steps_dir / "y-mt-extract"
    record(
        _run_cli_step(
            args,
            name="Y and mitochondrial BAM extraction",
            slug="14d-y-mt-extract",
            command_args=[
                "extract",
                "y-mt-extract",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(y_mt_dir),
            ],
            output_dir=y_mt_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                y_mt_dir / f"{base_name}_Y_MT.bam",
                y_mt_dir / f"{base_name}_Y_MT.bam.bai",
            ],
        )
    )

    unmapped_dir = steps_dir / "unmapped"
    record(
        _run_cli_step(
            args,
            name="Unmapped-read BAM extraction",
            slug="14e-unmapped",
            command_args=[
                "extract",
                "unmapped",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(unmapped_dir),
            ],
            output_dir=unmapped_dir,
            logs_dir=logs_dir,
            expected_outputs=[unmapped_dir / f"{base_name}_unmapped.bam"],
        )
    )

    custom_dir = steps_dir / "custom-extract"
    if heavy_region:
        record(
            _run_cli_step(
                args,
                name="Custom region BAM extraction",
                slug="14f-custom-extract",
                command_args=[
                    "extract",
                    "custom",
                    "--input",
                    str(analysis_bam),
                    "--ref",
                    str(ref_path),
                    "--outdir",
                    str(custom_dir),
                    "--region",
                    heavy_region,
                ],
                output_dir=custom_dir,
                logs_dir=logs_dir,
                expected_outputs=[
                    custom_dir
                    / f"{base_name}_{_region_output_suffix(heavy_region)}.bam",
                    custom_dir
                    / f"{base_name}_{_region_output_suffix(heavy_region)}.bam.bai",
                ],
            )
        )
    else:
        record(
            _skipped_result(
                "Custom region BAM extraction",
                "14f-custom-extract",
                custom_dir,
                "No benchmark region could be resolved from the generated reference.",
            )
        )

    fastp_dir = steps_dir / "fastp"
    if _benchmark_tool_available("fastp"):
        fastp_base = unalign_r1.name.split(".")[0]
        record(
            _run_cli_step(
                args,
                name="FASTQ trimming and QC with fastp",
                slug="15a-fastp",
                command_args=[
                    "qc",
                    "fastp",
                    "--r1",
                    str(unalign_r1),
                    "--r2",
                    str(unalign_r2),
                    "--outdir",
                    str(fastp_dir),
                ],
                output_dir=fastp_dir,
                logs_dir=logs_dir,
                expected_outputs=[
                    fastp_dir / f"{fastp_base}_fp_1.fastq.gz",
                    fastp_dir / f"{fastp_base}_fp_2.fastq.gz",
                    fastp_dir / f"{fastp_base}_fastp.json",
                    fastp_dir / f"{fastp_base}_fastp.html",
                ],
            )
        )
    else:
        record(_missing_optional_tool_result("fastp", "15a-fastp", fastp_dir))

    fastqc_dir = steps_dir / "fastqc"
    if _benchmark_tool_available("fastqc"):
        fastqc_base = _fastqc_output_stem(unalign_r1)
        record(
            _run_cli_step(
                args,
                name="FASTQ quality reports with FastQC",
                slug="15b-fastqc",
                command_args=[
                    "qc",
                    "fastqc",
                    "--input",
                    str(unalign_r1),
                    "--outdir",
                    str(fastqc_dir),
                ],
                output_dir=fastqc_dir,
                logs_dir=logs_dir,
                expected_outputs=[fastqc_dir / f"{fastqc_base}_fastqc.html"],
            )
        )
    else:
        record(_missing_optional_tool_result("fastqc", "15b-fastqc", fastqc_dir))

    filter_dir = steps_dir / "vcf-filter"
    filter_cmd = [
        "vcf",
        "filter",
        "--input",
        str(snp_vcf),
        "--ref",
        str(ref_path),
        "--outdir",
        str(filter_dir),
        "--expr",
        "QUAL>=0",
    ]
    if command_region:
        filter_cmd += ["--region", command_region]
    record(
        _run_cli_step(
            args,
            name="VCF expression filtering",
            slug="16a-vcf-filter",
            command_args=filter_cmd,
            output_dir=filter_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                filter_dir / "filtered.vcf.gz",
                filter_dir / "filtered.vcf.gz.tbi",
            ],
        )
    )

    annotate_dir = steps_dir / "vcf-annotate"
    record(
        _run_cli_step(
            args,
            name="VCF annotation transfer",
            slug="16b-vcf-annotate",
            command_args=[
                "vcf",
                "annotate",
                "--input",
                str(snp_vcf),
                "--ref",
                str(ref_path),
                "--outdir",
                str(annotate_dir),
                "--ann-vcf",
                str(target_tab_gz),
                "--cols",
                "CHROM,POS,REF,ALT,ID",
            ],
            output_dir=annotate_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                annotate_dir / "annotated.vcf.gz",
                annotate_dir / "annotated.vcf.gz.tbi",
            ],
        )
    )

    trio_dir = steps_dir / "vcf-trio"
    trio_proband = trio_dir / "proband.vcf.gz"
    trio_mother = trio_dir / "mother.vcf.gz"
    trio_father = trio_dir / "father.vcf.gz"
    trio_region = command_region or _trio_benchmark_region(ref_path) or heavy_region
    trio_prep = record(
        _run_internal_step(
            name="Prepare trio VCF benchmark inputs",
            slug="16c-vcf-trio-inputs",
            output_dir=trio_dir,
            func=lambda: _prepare_trio_vcf_inputs(
                snp_vcf,
                {
                    "proband": trio_proband,
                    "mother": trio_mother,
                    "father": trio_father,
                },
            ),
            expected_outputs=[
                trio_proband,
                Path(str(trio_proband) + ".tbi"),
                trio_mother,
                Path(str(trio_mother) + ".tbi"),
                trio_father,
                Path(str(trio_father) + ".tbi"),
            ],
        )
    )
    trio_cmd = [
        "vcf",
        "trio",
        "--proband",
        str(trio_proband),
        "--mother",
        str(trio_mother),
        "--father",
        str(trio_father),
        "--ref",
        str(ref_path),
        "--outdir",
        str(trio_dir),
        "--mode",
        "denovo",
    ]
    if trio_region:
        trio_cmd += ["--region", trio_region]
    if trio_prep.success:
        record(
            _run_cli_step(
                args,
                name="VCF trio inheritance filtering",
                slug="16d-vcf-trio",
                command_args=trio_cmd,
                output_dir=trio_dir,
                logs_dir=logs_dir,
                expected_outputs=[
                    trio_dir / "trio_denovo.vcf.gz",
                    trio_dir / "trio_denovo.vcf.gz.tbi",
                ],
            )
        )
    else:
        record(
            _skipped_result(
                "VCF trio inheritance filtering",
                "16d-vcf-trio",
                trio_dir,
                "Trio VCF input preparation failed.",
            )
        )

    freebayes_dir = steps_dir / "vcf-freebayes"
    if _benchmark_tool_available("freebayes"):
        freebayes_cmd = [
            "vcf",
            "freebayes",
            "--input",
            str(analysis_bam),
            "--ref",
            str(ref_path),
            "--outdir",
            str(freebayes_dir),
        ]
        if command_region:
            freebayes_cmd += ["--region", command_region]
        record(
            _run_cli_step(
                args,
                name="VCF generation with FreeBayes",
                slug="16e-vcf-freebayes",
                command_args=freebayes_cmd,
                output_dir=freebayes_dir,
                logs_dir=logs_dir,
                expected_outputs=[
                    freebayes_dir / "freebayes.vcf.gz",
                    freebayes_dir / "freebayes.vcf.gz.tbi",
                ],
            )
        )
    else:
        record(
            _missing_optional_tool_result(
                "freebayes", "16e-vcf-freebayes", freebayes_dir
            )
        )

    batch_fixture_dir = steps_dir / "analyze-batch-fixture"
    batch_csv = steps_dir / "analyze-batch-gen" / "benchmark_batch.csv"
    record(
        _run_internal_step(
            name="Prepare analyze batch-gen fixture",
            slug="17a-analyze-batch-fixture",
            output_dir=batch_fixture_dir,
            func=lambda: _prepare_analyze_batch_fixture(batch_fixture_dir),
            expected_outputs=[
                batch_fixture_dir / "benchmark_sample.bam",
                batch_fixture_dir / "benchmark_sample.vcf.gz",
            ],
        )
    )
    record(
        _run_cli_step(
            args,
            name="Analyze batch file generation",
            slug="17b-analyze-batch-gen",
            command_args=[
                "analyze",
                "batch-gen",
                "--directory",
                str(batch_fixture_dir),
                "--output",
                str(batch_csv),
            ],
            output_dir=batch_csv.parent,
            logs_dir=logs_dir,
            expected_outputs=[batch_csv],
        )
    )

    repair_dir = steps_dir / "repair"
    repair_bam_input = repair_dir / "ftdna_input.sam"
    repair_bam_output = repair_dir / "ftdna_repaired.sam"
    repair_vcf_input = repair_dir / "ftdna_input.vcf"
    repair_vcf_output = repair_dir / "ftdna_repaired.vcf"
    record(
        _run_internal_step(
            name="Prepare repair command fixtures",
            slug="18a-repair-fixtures",
            output_dir=repair_dir,
            func=lambda: _prepare_repair_fixtures(repair_bam_input, repair_vcf_input),
            expected_outputs=[repair_bam_input, repair_vcf_input],
        )
    )
    record(
        _run_cli_pipe_step(
            args,
            name="FTDNA BAM text repair",
            slug="18b-repair-ftdna-bam",
            command_args=["repair", "ftdna-bam"],
            input_file=repair_bam_input,
            output_file=repair_bam_output,
            output_dir=repair_dir,
            logs_dir=logs_dir,
            expected_outputs=[repair_bam_output],
        )
    )
    record(
        _run_cli_pipe_step(
            args,
            name="FTDNA VCF text repair",
            slug="18c-repair-ftdna-vcf",
            command_args=["repair", "ftdna-vcf"],
            input_file=repair_vcf_input,
            output_file=repair_vcf_output,
            output_dir=repair_dir,
            logs_dir=logs_dir,
            expected_outputs=[repair_vcf_output],
        )
    )


def _benchmark_root(args: argparse.Namespace) -> Path:
    explicit_dests: set[str] = getattr(args, "_explicit_dests", set())
    if "outdir" in explicit_dests and args.outdir:
        return Path(args.outdir).expanduser().resolve()
    return (Path.cwd() / "out" / "benchmark").resolve()


def _benchmark_thread_plan(args: argparse.Namespace) -> BenchmarkThreadPlan:
    if getattr(args, "threads", None) is not None:
        return BenchmarkThreadPlan(
            str(args.threads), int(args.threads), {}, "explicit --threads override"
        )

    tuning = default_thread_tuning_profile()
    return BenchmarkThreadPlan(tuning.label, tuning.threads, {}, tuning.reason)


def _run_cli_step(
    args: argparse.Namespace,
    name: str,
    slug: str,
    command_args: list[str],
    output_dir: Path,
    logs_dir: Path,
    expected_outputs: list[Path],
    command_label: str | None = None,
) -> BenchmarkResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{slug}.stdout.log"
    stderr_log = logs_dir / f"{slug}.stderr.log"
    command = _cli_command(args, command_args, _benchmark_threads_for_step(args, slug))
    start = time.perf_counter()

    with (
        open(stdout_log, "w", encoding="utf-8") as out,
        open(stderr_log, "w", encoding="utf-8") as err,
    ):
        completed = subprocess.run(
            command,
            stdout=out,
            stderr=err,
            check=False,
            text=True,
            env=_subprocess_env(),
        )

    seconds = time.perf_counter() - start
    missing = [str(path) for path in expected_outputs if not path.exists()]
    status = "PASS" if completed.returncode == 0 and not missing else "FAIL"
    error = None
    if completed.returncode != 0:
        error = f"Command exited with status {completed.returncode}."
    elif missing:
        error = "Missing expected output(s): " + ", ".join(missing)

    return BenchmarkResult(
        name=_name_with_command_label(name, command_args, command_label),
        slug=slug,
        status=status,
        seconds=seconds,
        command=command,
        output_dir=str(output_dir),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        returncode=completed.returncode,
        expected_outputs=[str(path) for path in expected_outputs],
        error=error,
    )


def _run_cli_pipe_step(
    args: argparse.Namespace,
    name: str,
    slug: str,
    command_args: list[str],
    input_file: Path,
    output_file: Path,
    output_dir: Path,
    logs_dir: Path,
    expected_outputs: list[Path],
    command_label: str | None = None,
) -> BenchmarkResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{slug}.stdout.log"
    stderr_log = logs_dir / f"{slug}.stderr.log"
    command = _cli_command(args, command_args, _benchmark_threads_for_step(args, slug))
    start = time.perf_counter()

    with (
        open(input_file, "rb") as stdin,
        open(output_file, "wb") as stdout,
        open(stderr_log, "w", encoding="utf-8") as err,
    ):
        completed = subprocess.run(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=err,
            check=False,
            env=_subprocess_env(),
        )

    stdout_log.write_text(str(output_file) + "\n", encoding="utf-8")
    seconds = time.perf_counter() - start
    missing = [str(path) for path in expected_outputs if not path.exists()]
    status = "PASS" if completed.returncode == 0 and not missing else "FAIL"
    error = None
    if completed.returncode != 0:
        error = f"Command exited with status {completed.returncode}."
    elif missing:
        error = "Missing expected output(s): " + ", ".join(missing)

    return BenchmarkResult(
        name=_name_with_command_label(name, command_args, command_label),
        slug=slug,
        status=status,
        seconds=seconds,
        command=command,
        output_dir=str(output_dir),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        returncode=completed.returncode,
        expected_outputs=[str(path) for path in expected_outputs],
        error=error,
    )


def _run_internal_step(
    name: str,
    slug: str,
    output_dir: Path,
    func: Any,
    expected_outputs: list[Path],
) -> BenchmarkResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    error = None
    try:
        func()
    except Exception as exc:
        error = str(exc)
    seconds = time.perf_counter() - start
    missing = [str(path) for path in expected_outputs if not path.exists()]
    if missing:
        missing_text = "Missing expected output(s): " + ", ".join(missing)
        error = f"{error}; {missing_text}" if error else missing_text
    status = "PASS" if error is None else "FAIL"
    return BenchmarkResult(
        name=name,
        slug=slug,
        status=status,
        seconds=seconds,
        command=["internal", slug],
        output_dir=str(output_dir),
        expected_outputs=[str(path) for path in expected_outputs],
        error=error,
    )


def _name_with_command_label(
    name: str, command_args: list[str], command_label: str | None = None
) -> str:
    label = command_label or _command_label(command_args)
    return f"{name} [{label}]" if label else name


def _command_label(command_args: list[str]) -> str | None:
    parts = []
    for arg in command_args:
        if arg.startswith("-"):
            break
        parts.append(arg)
    return " ".join(parts) if parts else None


def _skipped_result(
    name: str, slug: str, output_dir: Path, reason: str
) -> BenchmarkResult:
    return BenchmarkResult(
        name=name,
        slug=slug,
        status="SKIP",
        seconds=0.0,
        command=[],
        output_dir=str(output_dir),
        error=reason,
    )


def _missing_optional_tool_result(
    tool: str, slug: str, output_dir: Path
) -> BenchmarkResult:
    return _skipped_result(
        f"Optional {tool} benchmark",
        slug,
        output_dir,
        f"Optional tool is not installed or not active for this platform: {tool}.",
    )


def _prepare_real_benchmark_dataset(
    args: argparse.Namespace, dataset_dir: Path, outdir: Path
) -> BenchmarkDataset:
    zip_path = _real_dataset_zip_path(args, outdir)
    if getattr(args, "dataset_sha256", None):
        _verify_sha256(zip_path, str(args.dataset_sha256))

    extract_dir = dataset_dir / "real"
    _extract_zip_safely(zip_path, extract_dir)
    return _load_real_benchmark_dataset(extract_dir)


def _real_dataset_zip_path(args: argparse.Namespace, outdir: Path) -> Path:
    local_zip = getattr(args, "dataset_zip", None)
    if local_zip:
        path = Path(str(local_zip)).expanduser().resolve()
        if not path.is_file():
            raise WGSExtractError(f"Benchmark dataset zip not found: {path}")
        return path

    url = getattr(args, "dataset_url", None) or DEFAULT_REAL_DATASET_URL
    cache_dir = _real_dataset_cache_dir(args, outdir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / _download_filename(url)
    if zip_path.exists() and not getattr(args, "dataset_sha256", None):
        return zip_path
    if zip_path.exists() and _sha256(zip_path) == str(args.dataset_sha256).lower():
        return zip_path

    _download_file(url, zip_path)
    return zip_path


def _real_dataset_cache_dir(args: argparse.Namespace, outdir: Path) -> Path:
    cache_dir = getattr(args, "dataset_cache_dir", None)
    if cache_dir:
        return Path(str(cache_dir)).expanduser().resolve()
    return outdir / "datasets"


def _download_filename(url: str) -> str:
    filename = Path(url.split("?", 1)[0]).name
    if not filename:
        raise WGSExtractError(f"Dataset URL must end with a filename: {url}")
    return filename


def _download_file(url: str, destination: Path) -> None:
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "wgsextract-cli"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with open(tmp_path, "wb") as handle:
                shutil.copyfileobj(response, handle)
        tmp_path.replace(destination)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise WGSExtractError(
            f"Failed to download benchmark dataset {url}: {exc}"
        ) from exc


def _verify_sha256(path: Path, expected: str) -> None:
    normalized = expected.lower().strip()
    if not normalized:
        return
    actual = _sha256(path)
    if actual != normalized:
        raise WGSExtractError(
            f"Checksum mismatch for {path}: expected {normalized}, got {actual}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_zip_safely(zip_path: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    root = extract_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                target = (extract_dir / member.filename).resolve()
                if not _is_relative_to(target, root):
                    raise WGSExtractError(
                        f"Unsafe benchmark dataset zip entry: {member.filename}"
                    )
            archive.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise WGSExtractError(f"Invalid benchmark dataset zip: {zip_path}") from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_real_benchmark_dataset(root: Path) -> BenchmarkDataset:
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        candidates = list(root.glob("*/manifest.json"))
        if len(candidates) == 1:
            root = candidates[0].parent
            manifest_path = candidates[0]
    if not manifest_path.exists():
        raise WGSExtractError(f"Benchmark dataset manifest not found under {root}")

    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise WGSExtractError(
            f"Benchmark dataset manifest has no files object: {manifest_path}"
        )

    dataset = BenchmarkDataset(
        dataset_id=str(manifest.get("dataset_id") or root.name),
        description=str(
            manifest.get("description") or "release-backed real benchmark dataset"
        ),
        build=str(manifest.get("build") or "hg19"),
        root=root,
        ref=_required_dataset_file(root, files, "ref"),
        bam=_required_dataset_file(root, files, "bam"),
        bam_index=_optional_dataset_file(root, files, "bam_index"),
        cram=_optional_dataset_file(root, files, "cram"),
        cram_index=_optional_dataset_file(root, files, "cram_index"),
        fastq_r1=_optional_dataset_file(root, files, "fastq_r1"),
        fastq_r2=_optional_dataset_file(root, files, "fastq_r2"),
        vcf=_optional_dataset_file(root, files, "vcf"),
        vcf_index=_optional_dataset_file(root, files, "vcf_index"),
        targets=_optional_dataset_file(root, files, "targets"),
        targets_index=_optional_dataset_file(root, files, "targets_index"),
        default_region=manifest.get("default_region")
        if isinstance(manifest.get("default_region"), str)
        else None,
        manifest=manifest,
    )
    _validate_real_benchmark_dataset(dataset)
    return dataset


def _required_dataset_file(root: Path, files: dict[str, Any], role: str) -> Path:
    path = _optional_dataset_file(root, files, role)
    if path is None:
        raise WGSExtractError(
            f"Benchmark dataset is missing required file role: {role}"
        )
    return path


def _optional_dataset_file(root: Path, files: dict[str, Any], role: str) -> Path | None:
    value = files.get(role)
    if not isinstance(value, str) or not value:
        return None
    path = (root / value).resolve()
    if not _is_relative_to(path, root.resolve()):
        raise WGSExtractError(
            f"Benchmark dataset file role escapes dataset root: {role}"
        )
    return path


def _validate_real_benchmark_dataset(dataset: BenchmarkDataset) -> None:
    missing = [
        str(path)
        for path in _existing_dataset_outputs(dataset)
        if not path.exists() or not path.is_file()
    ]
    if missing:
        raise WGSExtractError(
            "Benchmark dataset is incomplete. Missing file(s): " + ", ".join(missing)
        )
    if dataset.fastq_r1 and not dataset.fastq_r2:
        raise WGSExtractError("Benchmark dataset provides fastq_r1 without fastq_r2.")


def _existing_dataset_outputs(dataset: BenchmarkDataset) -> list[Path]:
    outputs = [dataset.ref, dataset.bam]
    outputs.extend(
        path
        for path in (
            dataset.bam_index,
            dataset.cram,
            dataset.cram_index,
            dataset.fastq_r1,
            dataset.fastq_r2,
            dataset.vcf,
            dataset.vcf_index,
            dataset.targets,
            dataset.targets_index,
        )
        if path is not None
    )
    return outputs


def _align_output_stem(fastq_path: Path) -> str:
    return fastq_path.name.split(".")[0]


def _benchmark_tool_available(tool: str) -> bool:
    return _tool_active_for_benchmark(tool, get_tool_path(tool))


def _copy_bam_with_index(source_bam: Path, dest_bam: Path) -> None:
    dest_bam.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_bam, dest_bam)

    source_index = _bam_index_path(source_bam)
    dest_index = Path(str(dest_bam) + ".bai")
    if source_index and source_index.exists():
        shutil.copy2(source_index, dest_index)
    else:
        run_command(["samtools", "index", str(dest_bam)])


def _prepare_trio_vcf_inputs(source_vcf: Path, outputs: dict[str, Path]) -> None:
    for sample_name, output_vcf in outputs.items():
        sample_file = output_vcf.with_suffix(".sample.txt")
        sample_file.write_text(f"{sample_name}\n", encoding="utf-8")
        run_command(
            [
                "bcftools",
                "reheader",
                "-s",
                str(sample_file),
                "-o",
                str(output_vcf),
                str(source_vcf),
            ]
        )
        run_command(["tabix", "-f", "-p", "vcf", str(output_vcf)])


def _prepare_analyze_batch_fixture(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark_sample.bam").write_bytes(b"benchmark bam placeholder\n")
    (output_dir / "benchmark_sample.vcf.gz").write_bytes(b"benchmark vcf placeholder\n")
    (output_dir / "benchmark_sample.vcf.gz.tbi").write_bytes(b"benchmark index\n")


def _prepare_repair_fixtures(sam_path: Path, vcf_path: Path) -> None:
    sam_path.parent.mkdir(parents=True, exist_ok=True)
    sam_path.write_text(
        "@HD\tVN:1.6\tSO:coordinate\n"
        "@SQ\tSN:chr1\tLN:1000\n"
        "read name with spaces\t0\tchr1\t1\t60\t10M\t*\t0\t0\tACGTACGTAC\tIIIIIIIIII\n",
        encoding="utf-8",
    )
    vcf_path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t1\t.\tA\tC\t50\tPASS!BAD\t.\n",
        encoding="utf-8",
    )


def _assert_bam_unindexed(bam_path: Path) -> None:
    existing = [path for path in _bam_index_candidates(bam_path) if path.exists()]
    if existing:
        paths = ", ".join(str(path) for path in existing)
        raise WGSExtractError(f"BAM index still exists after unindex: {paths}")


def _bam_index_path(bam_path: Path) -> Path | None:
    for candidate in _bam_index_candidates(bam_path):
        if candidate.exists():
            return candidate
    return None


def _bam_index_candidates(bam_path: Path) -> list[Path]:
    candidates = [Path(str(bam_path) + ".bai"), Path(str(bam_path) + ".csi")]
    if bam_path.suffix.lower() == ".bam":
        candidates.append(bam_path.with_suffix(".bai"))
    return candidates


def _default_heavy_region(ref_path: Path) -> str | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    contigs = _read_fai(fai_path)
    if not contigs:
        return None
    chrom, length = contigs[0]
    end = min(length, 100_000)
    if end < 1:
        return None
    return f"{chrom}:1-{end}"


def _trio_benchmark_region(ref_path: Path) -> str | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    contigs = _read_fai(fai_path)
    for chrom, length in contigs:
        if chrom.upper().replace("CHR", "") in {"M", "MT"}:
            return f"{chrom}:1-{length}"
    return _default_heavy_region(ref_path)


def _region_output_suffix(region: str) -> str:
    return region.replace(":", "_").replace(",", "_")


def _chrom_only_region(region: str | None) -> str | None:
    if not region:
        return None
    chrom, _sep, _range_part = region.partition(":")
    return chrom or None


def _command_region(region: str | None, ref_path: Path) -> str | None:
    if not region:
        return None
    chrom, has_range, _range_part = region.partition(":")
    if has_range:
        return region
    length = _contig_length(ref_path, chrom)
    if length is None:
        return region
    return f"{chrom}:1-{length}"


def _contig_length(ref_path: Path, chrom: str) -> int | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    for contig, length in _read_fai(fai_path):
        if contig == chrom:
            return length
    return None


def _fastqc_output_stem(input_path: Path) -> str:
    name = input_path.name
    for suffix in (".fastq.gz", ".fq.gz", ".fastq", ".fq"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return input_path.stem


def _print_progress_header() -> None:
    print("WGSExtract CLI Benchmark Progress", flush=True)
    print(f"{'Step':<{PROGRESS_STEP_WIDTH}} {'Status':<6} {'Seconds':>10}", flush=True)
    print("-" * (PROGRESS_STEP_WIDTH + 19), flush=True)


def _print_thread_policy(thread_plan: BenchmarkThreadPlan) -> None:
    print("Thread policy:", flush=True)
    print(f"  Threads: {thread_plan.label}", flush=True)
    print(f"  Policy: {thread_plan.reason}", flush=True)
    print("", flush=True)


def _format_progress_result(result: BenchmarkResult) -> str:
    return f"{result.name:<{PROGRESS_STEP_WIDTH}} {result.status:<6} {result.seconds:>10.2f}"


def _print_failure_log_excerpt(result: BenchmarkResult) -> None:
    if not result.stderr_log:
        return
    stderr_path = Path(result.stderr_log)
    if not stderr_path.exists():
        return
    lines = stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return
    print(f"Failure stderr excerpt ({stderr_path}):", flush=True)
    for line in lines[-40:]:
        print(f"  {line}", flush=True)


def _benchmark_external_tools() -> list[dict[str, str | bool | None]]:
    tools: list[dict[str, str | bool | None]] = []
    for spec in BENCHMARK_EXTERNAL_TOOLS:
        path = get_tool_path(spec.name)
        version = get_tool_version(spec.name) if path else None
        active = _tool_active_for_benchmark(spec.name, path)
        tools.append(
            {
                "name": spec.name,
                "required": spec.required,
                "active": active,
                "status": _tool_status(spec.required, active, path),
                "purpose": spec.purpose,
                "path": path,
                "runtime": get_tool_runtime(path),
                "version": version,
            }
        )
    return tools


def _tool_active_for_benchmark(tool: str, path: str | None) -> bool:
    if path is None:
        return False
    if tool == "sambamba":
        return platform.system() != "Darwin"
    return True


def _tool_status(required: bool, active: bool, path: str | None) -> str:
    if active:
        return "active"
    if path:
        return "available, not used on this platform"
    if required:
        return "missing required tool"
    return "missing optional tool"


def _print_external_tools(tools: list[dict[str, str | bool | None]]) -> None:
    print("External tools used or checked by this benchmark:", flush=True)
    for tool in tools:
        print(f"  {_format_tool_line(tool)}", flush=True)
    print("", flush=True)


def _format_tool_line(tool: dict[str, str | bool | None]) -> str:
    requirement = "required" if tool["required"] else "optional"
    path = tool["path"] or "missing"
    runtime = tool["runtime"] or "missing"
    version = tool["version"] or "version unavailable"
    return (
        f"{tool['name']} [{requirement}, {tool['status']}] - {tool['purpose']} | "
        f"{runtime}: {path} | {version}"
    )


def _format_tool_names(tools: list[dict[str, str | bool | None]]) -> str:
    active_tools = [str(tool["name"]) for tool in tools if tool["active"]]
    return ", ".join(active_tools) if active_tools else "none"


def _format_machine_summary(stats: dict[str, str | int | None]) -> str:
    cores = _machine_stat_display_value(stats, "cores") or "unknown cores"
    ram = stats.get("ram_total") or "unknown RAM"
    disk_free = stats.get("disk_free") or "unknown disk free"
    os_name = stats.get("os") or "unknown OS"
    cpu = stats.get("cpu_model") or "unknown CPU"
    return f"{os_name} | {cpu} | {cores} | RAM {ram} | disk free {disk_free}"


def _machine_stats(run_dir: Path) -> dict[str, str | int | None]:
    virtual_memory = psutil.virtual_memory()
    disk_usage = psutil.disk_usage(str(run_dir))
    return {
        "os": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "architecture": platform.machine() or None,
        "cpu_model": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "cpu_frequency": _cpu_frequency(),
        "ram_total": _format_bytes(virtual_memory.total),
        "ram_available": _format_bytes(virtual_memory.available),
        "ram_speed": _ram_speed(),
        "benchmark_filesystem": str(run_dir.anchor or run_dir),
        "disk_total": _format_bytes(disk_usage.total),
        "disk_free": _format_bytes(disk_usage.free),
        "drive_model": _drive_model(run_dir),
        "drive_speed": _drive_speed(run_dir),
    }


def _cpu_frequency() -> str | None:
    cpu_freq = getattr(psutil, "cpu_freq", None)
    if not cpu_freq:
        return None
    try:
        frequency = cpu_freq()
    except Exception:
        return None
    return _format_cpu_frequency(frequency.current) if frequency else None


def _print_machine_stats(stats: dict[str, str | int | None]) -> None:
    print("Machine stats:", flush=True)
    for label, key in (
        ("OS", "os"),
        ("Architecture", "architecture"),
        ("CPU", "cpu_model"),
        ("Cores", "cores"),
        ("CPU frequency", "cpu_frequency"),
        ("RAM", "ram"),
        ("RAM speed", "ram_speed"),
        ("Benchmark filesystem", "benchmark_filesystem"),
        ("Disk", "disk"),
        ("Drive", "drive"),
        ("Drive speed", "drive_speed"),
    ):
        value = _machine_stat_display_value(stats, key)
        if value:
            print(f"  {label}: {value}", flush=True)
    print("", flush=True)


def _machine_stat_display_value(
    stats: dict[str, str | int | None], key: str
) -> str | None:
    if key == "cores":
        physical = stats["physical_cores"] or "unknown"
        logical = stats["logical_cores"] or "unknown"
        return f"{physical} physical / {logical} logical"
    if key == "ram":
        return f"{stats['ram_total']} total / {stats['ram_available']} available"
    if key == "disk":
        return f"{stats['disk_total']} total / {stats['disk_free']} free"
    if key == "drive":
        drive_model = stats["drive_model"]
        return str(drive_model) if drive_model else None
    value = stats.get(key)
    return str(value) if value else None


def _cpu_model() -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _command_output(["sysctl", "-n", "machdep.cpu.brand_string"])
    if system == "Windows":
        return platform.processor() or _command_output(
            ["wmic", "cpu", "get", "Name", "/value"]
        )
    if system == "Linux":
        return _linux_cpu_model()
    return platform.processor() or None


def _linux_cpu_model() -> str | None:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return platform.processor() or None
    try:
        with open(cpuinfo, encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    _key, _sep, value = line.partition(":")
                    return value.strip() or None
    except OSError:
        return platform.processor() or None
    return platform.processor() or None


def _ram_speed() -> str | None:
    system = platform.system()
    if system == "Darwin":
        speed = _command_output(["system_profiler", "SPMemoryDataType"])
        return _first_matching_line(speed, "Speed:")
    if system == "Windows":
        return _command_output(
            ["wmic", "memorychip", "get", "Speed", "/value"], first_value=True
        )
    if system == "Linux":
        return _command_output(["dmidecode", "-t", "memory"], match="Speed:")
    return None


def _drive_model(path: Path) -> str | None:
    system = platform.system()
    if system == "Darwin":
        output = _command_output(["diskutil", "info", _filesystem_mount(path)])
        model = _first_matching_line(output, "Device / Media Name:")
        if model:
            return model
        model = _first_matching_line(output, "Media Name:")
        if model:
            return model
        return _first_matching_line(
            _macos_storage_profile_for_path(path), "Device Name:"
        )
    if system == "Windows":
        return _command_output(
            ["wmic", "diskdrive", "get", "Model", "/value"], first_value=True
        )
    if system == "Linux":
        return _linux_drive_model(path)
    return None


def _linux_drive_model(path: Path) -> str | None:
    device = _command_output(["df", "--output=source", str(path)])
    if not device:
        return None
    lines = [line.strip() for line in device.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    block_name = Path(lines[-1]).name
    while block_name and not (Path("/sys/block") / block_name).exists():
        block_name = block_name[:-1]
    model_path = Path("/sys/block") / block_name / "device" / "model"
    if model_path.exists():
        try:
            return model_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _drive_speed(path: Path) -> str | None:
    system = platform.system()
    if system == "Darwin":
        output = _command_output(["diskutil", "info", _filesystem_mount(path)])
        protocol = _first_matching_line(output, "Protocol:")
        if protocol:
            return protocol
        profile = _macos_storage_profile_for_path(path)
        medium = _first_matching_line(profile, "Medium Type:")
        protocol = _first_matching_line(profile, "Protocol:")
        if medium and protocol:
            return f"{medium}, {protocol}"
        return medium or protocol
    if system == "Windows":
        return _command_output(
            ["wmic", "diskdrive", "get", "MediaType,InterfaceType", "/value"],
            first_value=True,
        )
    if system == "Linux":
        rotational = _linux_drive_rotational(path)
        if rotational is None:
            return None
        return "HDD/rotational" if rotational else "SSD/non-rotational"
    return None


def _linux_drive_rotational(path: Path) -> bool | None:
    device = _command_output(["df", "--output=source", str(path)])
    if not device:
        return None
    lines = [line.strip() for line in device.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    block_name = Path(lines[-1]).name
    while block_name and not (Path("/sys/block") / block_name).exists():
        block_name = block_name[:-1]
    rotational_path = Path("/sys/block") / block_name / "queue" / "rotational"
    if not rotational_path.exists():
        return None
    try:
        return rotational_path.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return None


def _format_cpu_frequency(mhz: float) -> str:
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def _filesystem_mount(path: Path) -> str:
    try:
        partitions = sorted(
            psutil.disk_partitions(all=True),
            key=lambda partition: len(partition.mountpoint),
            reverse=True,
        )
        resolved = str(path.resolve())
        for partition in partitions:
            if resolved == partition.mountpoint or resolved.startswith(
                partition.mountpoint.rstrip(os.sep) + os.sep
            ):
                return str(partition.mountpoint)
    except OSError:
        pass
    return str(path)


def _macos_storage_profile_for_path(path: Path) -> str | None:
    mount = _filesystem_mount(path)
    output = _command_output(["system_profiler", "SPStorageDataType"])
    if not output:
        return None
    blocks = output.split("\n\n")
    for block in blocks:
        if f"Mount Point: {mount}" in block:
            return block
    return None


def _command_output(
    command: list[str], match: str | None = None, first_value: bool = False
) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if match:
        return _first_matching_line(output, match)
    if first_value:
        return _first_value_line(output)
    return output or None


def _first_matching_line(output: str | None, prefix: str) -> str | None:
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            _key, _sep, value = stripped.partition(":")
            return value.strip() or None
    return None


def _first_value_line(output: str | None) -> str | None:
    if not output:
        return None
    values: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" in stripped:
            _key, _sep, value = stripped.partition("=")
            if value.strip():
                values.append(value.strip())
        elif not stripped.lower().startswith(
            ("model", "speed", "mediatype", "interfacetype")
        ):
            values.append(stripped)
    return ", ".join(values[:3]) if values else None


def _record_base_file_size(metadata: dict[str, Any], base_file: Path) -> None:
    if not base_file.exists():
        metadata["base_file_size"] = "not available"
        metadata["base_file_size_bytes"] = None
        return

    size_bytes = base_file.stat().st_size
    metadata["base_file_size"] = _format_bytes(size_bytes)
    metadata["base_file_size_bytes"] = size_bytes


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "bytes":
                return f"{size_bytes:,} bytes"
            return f"{size:.1f} {unit} ({size_bytes:,} bytes)"
        size /= 1024
    return f"{size_bytes:,} bytes"


def _benchmark_threads_for_step(args: argparse.Namespace, slug: str) -> int | None:
    thread_plan = getattr(args, "_benchmark_thread_plan", None)
    if isinstance(thread_plan, BenchmarkThreadPlan):
        return thread_plan.per_step_threads.get(slug, thread_plan.default_threads)
    return getattr(args, "threads", None)


def _cli_command(
    args: argparse.Namespace, command_args: list[str], threads: int | None
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "wgsextract_cli.main",
        "--parent-pid",
        str(os.getpid()),
    ]
    if getattr(args, "debug", False):
        command.append("--debug")
    elif getattr(args, "quiet", False):
        command.append("--quiet")
    command += command_args
    if threads is not None:
        command += ["--threads", str(threads)]
    if getattr(args, "memory", None) is not None:
        command += ["--memory", str(args.memory)]
    return command


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parents[2]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing}" if existing else str(src_dir)
    return env


def _create_target_snp_tab(
    ref_path: Path, target_tab_gz: Path, target_count: int, region: str | None
) -> None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        run_command(["samtools", "faidx", str(ref_path)])

    contigs = _read_fai(fai_path)
    ranges = _target_ranges(contigs, region)
    if not ranges:
        raise WGSExtractError(f"No reference contigs match benchmark region: {region}")

    total_bases = sum(end - start + 1 for _name, start, end in ranges)
    tab_path = target_tab_gz.with_suffix("")
    bases = ["A", "C", "G", "T"]

    with open(tab_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#CHROM\tPOS\tID\tREF\tALT\n")
        snp_index = 1
        for chrom, start, end in ranges:
            length = end - start + 1
            contig_targets = max(1, round(target_count * (length / total_bases)))
            step = max(1, length // (contig_targets + 1))
            for offset in range(step, length, step):
                if snp_index > target_count and len(ranges) == 1:
                    break
                pos = start + offset
                ref = bases[(snp_index + len(chrom)) % len(bases)]
                alt = bases[(snp_index + len(chrom) + 1) % len(bases)]
                if ref == alt:
                    alt = bases[(bases.index(ref) + 1) % len(bases)]
                handle.write(f"{chrom}\t{pos}\tbench_rs{snp_index}\t{ref}\t{alt}\n")
                snp_index += 1

    run_command(["bgzip", "-f", str(tab_path)])
    run_command(["tabix", "-f", "-p", "vcf", str(target_tab_gz)])


def _read_fai(fai_path: Path) -> list[tuple[str, int]]:
    contigs: list[tuple[str, int]] = []
    with open(fai_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                contigs.append((parts[0], int(parts[1])))
    return contigs


def _target_ranges(
    contigs: list[tuple[str, int]], region: str | None
) -> list[tuple[str, int, int]]:
    if not region:
        return [(chrom, 1, length) for chrom, length in contigs]

    chrom_part, has_range, range_part = region.partition(":")
    matching = [(chrom, length) for chrom, length in contigs if chrom == chrom_part]
    if not matching and chrom_part.startswith("chr"):
        matching = [
            (chrom, length) for chrom, length in contigs if chrom == chrom_part[3:]
        ]
    if not matching and not chrom_part.startswith("chr"):
        matching = [
            (chrom, length) for chrom, length in contigs if chrom == f"chr{chrom_part}"
        ]

    ranges = []
    for chrom, length in matching:
        start = 1
        end = length
        if has_range:
            raw_start, _sep, raw_end = range_part.replace(",", "").partition("-")
            start = max(1, int(raw_start))
            end = min(length, int(raw_end) if raw_end else length)
        if start <= end:
            ranges.append((chrom, start, end))
    return ranges


def _ploidy_for_build(build: str) -> str:
    if build in {"hg19", "hg37"}:
        return "GRCh37"
    return "GRCh38"


def _write_report(
    run_dir: Path, metadata: dict[str, Any], results: list[BenchmarkResult]
) -> None:
    report_md = run_dir / "benchmark_report.md"
    report_json = run_dir / "benchmark_results.json"
    payload = {
        "metadata": metadata,
        "system": _system_metadata(),
        "results": [asdict(result) for result in results],
    }
    with open(report_json, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    stdout_report = _format_stdout_report(metadata, results, report_md)
    markdown_report = _format_markdown_report(metadata, results, report_json)
    with open(report_md, "w", encoding="utf-8") as handle:
        handle.write(markdown_report)

    print(stdout_report)


def _system_metadata() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "executable": sys.executable,
    }


def _format_stdout_report(
    metadata: dict[str, Any], results: list[BenchmarkResult], report_md: Path
) -> str:
    total = sum(result.seconds for result in results if result.status != "SKIP")
    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")

    lines = [
        "",
        "WGSExtract CLI Benchmark Summary",
        f"Profile: {metadata['profile']} | Suite: {metadata['suite']} | "
        f"Coverage: {metadata['coverage']}x | Full size: {metadata['full_size']}",
        f"Tool runtime: {metadata['tool_runtime']}",
        f"Data source: {metadata['data_source']} ({metadata['data_source_description']})",
        f"Fake BAM generator: {metadata['fake_bam_generator']}",
        f"Threads: {metadata['threads']}",
        f"Thread policy: {metadata['thread_policy']}",
        f"Region: {metadata['region'] or 'whole generated genome'} | Seed: {metadata['seed']}",
        f"Base file: {metadata['base_file']} ({metadata['base_file_size'] or 'not available'})",
        "Machine: " + _format_machine_summary(metadata["machine_stats"]),
        "External tools: " + _format_tool_names(metadata["external_tools"]),
        f"Excluded operations: {metadata['excluded_operations']}",
    ]
    lines += [
        f"Total measured time: {total:.2f}s",
        f"Passed: {passed} | Failed: {failed} | Skipped: {skipped}",
        f"Markdown report: {report_md}",
    ]
    return "\n".join(lines)


def _format_markdown_report(
    metadata: dict[str, Any], results: list[BenchmarkResult], report_json: Path
) -> str:
    total = sum(result.seconds for result in results if result.status != "SKIP")
    lines = [
        "# WGSExtract CLI Benchmark Report",
        "",
        "## Configuration",
        "",
        f"- Profile: `{metadata['profile']}`",
        f"- Suite: `{metadata['suite']}`",
        f"- Coverage: `{metadata['coverage']}x`",
        f"- Tool runtime: `{metadata['tool_runtime']}`",
        f"- Full size reference: `{metadata['full_size']}`",
        f"- Data source: `{metadata['data_source']}` ({metadata['data_source_description']})",
        f"- Fake BAM generator: `{metadata['fake_bam_generator']}`",
        f"- Build: `{metadata['build']}`",
        f"- Region: `{metadata['region'] or 'whole generated genome'}`",
        f"- Seed: `{metadata['seed']}`",
        f"- Target SNP count: `{metadata['target_count']}`",
        f"- Threads: `{metadata['threads']}`",
        f"- Thread policy: `{metadata['thread_policy']}`",
        f"- Base file: `{metadata['base_file']}` ({metadata['base_file_size'] or 'not available'})",
        f"- Excluded operations: {metadata['excluded_operations']}",
        f"- JSON results: `{report_json}`",
        "",
        "## Machine Stats",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    for label, key in (
        ("OS", "os"),
        ("Architecture", "architecture"),
        ("CPU", "cpu_model"),
        ("Cores", "cores"),
        ("CPU frequency", "cpu_frequency"),
        ("RAM", "ram"),
        ("RAM speed", "ram_speed"),
        ("Benchmark filesystem", "benchmark_filesystem"),
        ("Disk", "disk"),
        ("Drive", "drive"),
        ("Drive speed/type", "drive_speed"),
        ("Python", "python"),
    ):
        value = _machine_stat_display_value(metadata["machine_stats"], key)
        if value:
            lines.append(f"| {label} | {value} |")

    lines += [
        "",
        "## External Tools",
        "",
        "| Tool | Required | Status | Runtime | Path | Version | Purpose |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for tool in metadata["external_tools"]:
        lines.append(
            "| "
            f"{tool['name']} | "
            f"{tool['required']} | "
            f"{tool['status']} | "
            f"{tool['runtime'] or 'missing'} | "
            f"`{tool['path'] or 'missing'}` | "
            f"{tool['version'] or 'version unavailable'} | "
            f"{tool['purpose']} |"
        )

    lines += [
        "",
        "## Summary",
        "",
        "| Step | Status | Seconds | Output Directory |",
        "| --- | --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.status} | {result.seconds:.2f} | `{result.output_dir}` |"
        )
    lines += ["", f"Total measured time: **{total:.2f}s**", "", "## Details", ""]

    for result in results:
        lines += [
            f"### {result.name}",
            "",
            f"- Status: `{result.status}`",
            f"- Duration: `{result.seconds:.2f}s`",
            f"- Output directory: `{result.output_dir}`",
        ]
        if result.command:
            lines.append(f"- Command: `{shlex.join(result.command)}`")
        if result.returncode is not None:
            lines.append(f"- Return code: `{result.returncode}`")
        if result.stdout_log:
            lines.append(f"- Stdout log: `{result.stdout_log}`")
        if result.stderr_log:
            lines.append(f"- Stderr log: `{result.stderr_log}`")
        if result.expected_outputs:
            outputs = ", ".join(f"`{path}`" for path in result.expected_outputs)
            lines.append(f"- Expected outputs: {outputs}")
        if result.error:
            lines.append(f"- Error: {result.error}")
        lines.append("")

    return "\n".join(lines)
