import argparse
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import IO, TextIO

from wgsextract_cli.core.config import config_args

from .models import (
    BenchmarkResult,
    BenchmarkThreadPlan,
    _name_with_command_label,
)


def _benchmark_threads(args: argparse.Namespace) -> int | None:
    thread_plan = getattr(args, "_benchmark_thread_plan", None)
    if isinstance(thread_plan, BenchmarkThreadPlan):
        return thread_plan.default_threads
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
        *config_args(),
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
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{slug}.stdout.log"
    stderr_log = logs_dir / f"{slug}.stderr.log"
    command = _cli_command(args, command_args, _benchmark_threads(args))
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
    completed: subprocess.CompletedProcess[object],
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
) -> subprocess.CompletedProcess[object]:
    with ExitStack() as stack:
        stdin: IO[bytes] | None = (
            stack.enter_context(open(stdin_file, "rb")) if stdin_file else None
        )
        if stdout_file:
            stdout: IO[bytes] | TextIO | None = stack.enter_context(
                open(stdout_file, "wb")
            )
        elif stdout_log:
            stdout = stack.enter_context(
                open(stdout_log, "w", encoding="utf-8", newline="\n")
            )
        else:
            stdout = None
        stderr = stack.enter_context(
            open(stderr_log, "w", encoding="utf-8", newline="\n")
        )
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
        stdout_log.write_text(stdout_log_text, encoding="utf-8", newline="\n")
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
    func: Callable[[], object],
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
