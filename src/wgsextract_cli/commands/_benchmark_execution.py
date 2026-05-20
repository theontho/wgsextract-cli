import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)

from ._benchmark_models import (
    BenchmarkResult,
    BenchmarkThreadPlan,
    _name_with_command_label,
)


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


def _cli_step_context(
    args: argparse.Namespace,
    slug: str,
    command_args: list[str],
    output_dir: Path,
    logs_dir: Path,
) -> tuple[Path, Path, list[str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{slug}.stdout.log"
    stderr_log = logs_dir / f"{slug}.stderr.log"
    command = _cli_command(args, command_args, _benchmark_threads_for_step(args, slug))
    return stdout_log, stderr_log, command


def _cli_step_result(
    *,
    name: str,
    slug: str,
    command_args: list[str],
    command_label: str | None,
    seconds: float,
    command: list[str],
    output_dir: Path,
    stdout_log: Path,
    stderr_log: Path,
    returncode: int,
    expected_outputs: list[Path],
) -> BenchmarkResult:
    missing = [str(path) for path in expected_outputs if not path.exists()]
    status = "PASS" if returncode == 0 and not missing else "FAIL"
    error = None
    if returncode != 0:
        error = f"Command exited with status {returncode}."
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
        returncode=returncode,
        expected_outputs=[str(path) for path in expected_outputs],
        error=error,
    )


def _completed_cli_step_result(
    *,
    name: str,
    slug: str,
    command_args: list[str],
    command_label: str | None,
    start: float,
    completed: subprocess.CompletedProcess[Any],
    command: list[str],
    output_dir: Path,
    stdout_log: Path,
    stderr_log: Path,
    expected_outputs: list[Path],
) -> BenchmarkResult:
    return _cli_step_result(
        name=name,
        slug=slug,
        command_args=command_args,
        command_label=command_label,
        seconds=time.perf_counter() - start,
        command=command,
        output_dir=output_dir,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        returncode=completed.returncode,
        expected_outputs=expected_outputs,
    )


def _run_cli_subprocess(
    command: list[str],
    stderr_log: Path,
    *,
    stdout_log: Path | None = None,
    stdin_file: Path | None = None,
    stdout_file: Path | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess[Any]:
    with ExitStack() as stack:
        stdin: Any = stack.enter_context(open(stdin_file, "rb")) if stdin_file else None
        if stdout_file:
            stdout: Any = stack.enter_context(open(stdout_file, "wb"))
        elif stdout_log:
            stdout = stack.enter_context(open(stdout_log, "w", encoding="utf-8"))
        else:
            stdout = None
        stderr = stack.enter_context(open(stderr_log, "w", encoding="utf-8"))
        return subprocess.run(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            check=False,
            text=text,
            env=_subprocess_env(),
        )


def _run_cli_step_with_io(
    args: argparse.Namespace,
    name: str,
    slug: str,
    command_args: list[str],
    output_dir: Path,
    logs_dir: Path,
    expected_outputs: list[Path],
    command_label: str | None = None,
    stdin_file: Path | None = None,
    stdout_file: Path | None = None,
    stdout_log_text: str | None = None,
    text: bool = False,
) -> BenchmarkResult:
    stdout_log, stderr_log, command = _cli_step_context(
        args, slug, command_args, output_dir, logs_dir
    )
    start = time.perf_counter()
    completed = _run_cli_subprocess(
        command,
        stderr_log,
        stdout_log=None if stdout_file else stdout_log,
        stdin_file=stdin_file,
        stdout_file=stdout_file,
        text=text,
    )
    if stdout_log_text is not None:
        stdout_log.write_text(stdout_log_text, encoding="utf-8")
    return _completed_cli_step_result(
        name=name,
        slug=slug,
        command_args=command_args,
        command_label=command_label,
        start=start,
        completed=completed,
        command=command,
        output_dir=output_dir,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        expected_outputs=expected_outputs,
    )


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
    return _run_cli_step_with_io(
        args,
        name,
        slug,
        command_args,
        output_dir,
        logs_dir,
        expected_outputs,
        command_label,
        text=True,
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


def _bam_index_candidates(bam_path: Path) -> list[Path]:
    candidates = [Path(str(bam_path) + ".bai"), Path(str(bam_path) + ".csi")]
    if bam_path.suffix.lower() == ".bam":
        candidates.append(bam_path.with_suffix(".bai"))
    return candidates


def _bam_index_path(bam_path: Path) -> Path | None:
    for candidate in _bam_index_candidates(bam_path):
        if candidate.exists():
            return candidate
    return None


def _copy_bam_with_index(source_bam: Path, dest_bam: Path) -> None:
    dest_bam.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_bam, dest_bam)

    source_index = _bam_index_path(source_bam)
    if source_index and source_index.exists():
        dest_index = _copied_bam_index_path(source_bam, dest_bam)
        shutil.copy2(source_index, dest_index)
    else:
        run_command(["samtools", "index", str(dest_bam)])


def _copied_bam_index_path(source_bam: Path, dest_bam: Path) -> Path:
    source_index = _bam_index_path(source_bam)
    if source_index and source_index.name == source_bam.name + source_index.suffix:
        return Path(str(dest_bam) + source_index.suffix)
    if source_index:
        return dest_bam.with_suffix(source_index.suffix)
    return Path(str(dest_bam) + ".bai")


def _assert_bam_unindexed(bam_path: Path) -> None:
    existing = [path for path in _bam_index_candidates(bam_path) if path.exists()]
    if existing:
        paths = ", ".join(str(path) for path in existing)
        raise WGSExtractError(f"BAM index still exists after unindex: {paths}")


def _run_heavy_reference_and_bam_steps(
    *,
    args: argparse.Namespace,
    record: Any,
    analysis_bam: Path,
    ref_path: Path,
    steps_dir: Path,
    logs_dir: Path,
) -> None:
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
            expected_outputs=[
                index_fixture,
                _copied_bam_index_path(analysis_bam, index_fixture),
            ],
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


def _tool_active_for_benchmark(tool: str, path: str | None) -> bool:
    if path is None:
        return False
    if tool == "sambamba":
        return platform.system() != "Darwin"
    return True
