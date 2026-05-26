import argparse
from pathlib import Path

from wgsextract_cli.core.utils import run_command

from .execution import (
    _run_cli_step_with_io,
)
from .models import (
    BenchmarkResult,
    _default_heavy_region,
    _read_fai,
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
    return _run_cli_step_with_io(
        args,
        name,
        slug,
        command_args,
        output_dir,
        logs_dir,
        expected_outputs,
        command_label,
        stdin_file=input_file,
        stdout_file=output_file,
        stdout_log_text=str(output_file) + "\n",
    )


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


def _trio_benchmark_region(ref_path: Path) -> str | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    contigs = _read_fai(fai_path)
    for chrom, length in contigs:
        if chrom.upper().replace("CHR", "") in {"M", "MT"}:
            return f"{chrom}:1-{length}"
    return _default_heavy_region(ref_path)
