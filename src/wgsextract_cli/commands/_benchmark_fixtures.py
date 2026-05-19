import argparse
import subprocess
import time
from pathlib import Path

from wgsextract_cli.core.utils import run_command

from ._benchmark_execution import (
    _benchmark_threads_for_step,
    _cli_command,
    _subprocess_env,
)
from ._benchmark_models import (
    BenchmarkResult,
    _default_heavy_region,
    _name_with_command_label,
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
