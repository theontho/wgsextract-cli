import argparse
from collections.abc import Callable
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path

from .execution import (
    _missing_optional_tool_result,
    _run_cli_step,
    _skipped_result,
    _tool_active_for_benchmark,
)
from .models import BenchmarkResult


def _benchmark_tool_available(tool: str) -> bool:
    return _tool_active_for_benchmark(tool, get_tool_path(tool))


def _region_output_suffix(region: str) -> str:
    return region.replace(":", "_").replace(",", "_")


def _fastqc_output_stem(input_path: Path) -> str:
    name = input_path.name
    for suffix in (".fastq.gz", ".fq.gz", ".fastq", ".fq"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return input_path.stem


def _run_heavy_coverage_extract_and_qc_steps(
    *,
    args: argparse.Namespace,
    record: Callable[[BenchmarkResult], BenchmarkResult],
    analysis_bam: Path,
    ref_path: Path,
    unalign_r1: Path,
    unalign_r2: Path,
    steps_dir: Path,
    logs_dir: Path,
    base_name: str,
    heavy_region: str | None,
    command_region: str | None,
    command_chrom: str | None,
) -> None:
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
