import argparse
from pathlib import Path
from typing import Any

from wgsextract_cli.core.runtime import default_thread_tuning_profile

from ._benchmark_execution import (
    _missing_optional_tool_result,
    _run_cli_step,
    _run_heavy_reference_and_bam_steps,
    _run_internal_step,
    _skipped_result,
)
from ._benchmark_fixtures import (
    _prepare_analyze_batch_fixture,
    _prepare_repair_fixtures,
    _prepare_trio_vcf_inputs,
    _run_cli_pipe_step,
    _trio_benchmark_region,
)
from ._benchmark_models import (
    BenchmarkThreadPlan,
    _chrom_only_region,
    _command_region,
    _default_heavy_region,
)
from ._benchmark_qc import (
    _benchmark_tool_available,
    _run_heavy_coverage_extract_and_qc_steps,
)


def _run_heavy_vcf_analysis_and_repair_steps(
    *,
    args: argparse.Namespace,
    record: Any,
    analysis_bam: Path,
    ref_path: Path,
    target_tab_gz: Path,
    snp_vcf: Path,
    steps_dir: Path,
    logs_dir: Path,
    command_region: str | None,
    heavy_region: str | None,
) -> None:
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

    _run_heavy_reference_and_bam_steps(
        args=args,
        record=record,
        analysis_bam=analysis_bam,
        ref_path=ref_path,
        steps_dir=steps_dir,
        logs_dir=logs_dir,
    )
    _run_heavy_coverage_extract_and_qc_steps(
        args=args,
        record=record,
        analysis_bam=analysis_bam,
        ref_path=ref_path,
        unalign_r1=unalign_r1,
        unalign_r2=unalign_r2,
        steps_dir=steps_dir,
        logs_dir=logs_dir,
        base_name=base_name,
        heavy_region=heavy_region,
        command_region=command_region,
        command_chrom=command_chrom,
    )
    _run_heavy_vcf_analysis_and_repair_steps(
        args=args,
        record=record,
        analysis_bam=analysis_bam,
        ref_path=ref_path,
        target_tab_gz=target_tab_gz,
        snp_vcf=snp_vcf,
        steps_dir=steps_dir,
        logs_dir=logs_dir,
        command_region=command_region,
        heavy_region=heavy_region,
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


def _verified_checksum_path(path: Path, checksum: str | None) -> Path:
    suffix = checksum.lower().strip() if checksum else "unchecked"
    return path.with_name(f".{path.name}.{suffix}.verified")
