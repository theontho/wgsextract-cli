import argparse
from collections.abc import Callable
from pathlib import Path

from ._benchmark_execution import (
    _run_cli_step,
    _skipped_result,
)
from ._benchmark_models import (
    BenchmarkDataset,
    BenchmarkResult,
)
from ._benchmark_setup import (
    _ploidy_for_build,
    _run_core_benchmark_setup_steps,
)


def _run_core_benchmark_analysis_steps(
    *,
    args: argparse.Namespace,
    record: Callable[[BenchmarkResult], BenchmarkResult],
    real_dataset: BenchmarkDataset | None,
    steps_dir: Path,
    logs_dir: Path,
    analysis_bam: Path,
    ref_path: Path,
    target_tab_gz: Path,
    benchmark_build: str,
    region: str | None,
    region_allowed: bool,
) -> Path:
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
    if region and region_allowed:
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
    if region and region_allowed:
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
    if region and region_allowed:
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
    if region and region_allowed:
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
    return snp_vcf


def _run_core_benchmark_steps(
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
) -> tuple[Path, Path, Path, Path]:
    analysis_bam, unalign_r1, unalign_r2 = _run_core_benchmark_setup_steps(
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
    snp_vcf = _run_core_benchmark_analysis_steps(
        args=args,
        record=record,
        real_dataset=real_dataset,
        steps_dir=steps_dir,
        logs_dir=logs_dir,
        analysis_bam=analysis_bam,
        ref_path=ref_path,
        target_tab_gz=target_tab_gz,
        benchmark_build=benchmark_build,
        region=region,
        region_allowed=region_allowed,
    )
    return analysis_bam, snp_vcf, unalign_r1, unalign_r2
