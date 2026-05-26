import argparse
from collections.abc import Callable
from pathlib import Path

from wgsextract_cli.core.builds import (
    ploidy_for_build,
)

from ._benchmark_datasets import (
    _existing_dataset_outputs,
)
from ._benchmark_execution import (
    _run_cli_step,
    _run_internal_step,
)
from ._benchmark_models import (
    BenchmarkDataset,
    BenchmarkResult,
)
from ._benchmark_reports import (
    _align_output_stem,
    _create_target_snp_tab,
)


def _run_core_benchmark_setup_steps(
    *,
    args: argparse.Namespace,
    record: Callable[[BenchmarkResult], BenchmarkResult],
    real_dataset: BenchmarkDataset | None,
    dataset_dir: Path,
    steps_dir: Path,
    logs_dir: Path,
    generated_bam: Path,
    ref_path: Path,
    target_tab_gz: Path,
    benchmark_build: str,
    coverage: float,
    full_size: bool,
    target_count: int,
    region: str | None,
    region_allowed: bool,
) -> tuple[Path, Path, Path]:
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
    if region and region_allowed:
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
    return analysis_bam, unalign_r1, unalign_r2


def _ploidy_for_build(build: str) -> str:
    return ploidy_for_build(build)
