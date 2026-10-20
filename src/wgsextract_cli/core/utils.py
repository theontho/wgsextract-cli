import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from typing import IO, Literal, TypeAlias, overload

from wgsextract_cli.core.process_registry import (
    cleanup_processes as cleanup_processes,
)
from wgsextract_cli.core.process_registry import (
    proc_registry,
    process_group_kwargs,
)
from wgsextract_cli.core.resource_policy import (
    get_resource_defaults as _get_resource_defaults,
)
from wgsextract_cli.core.runtime import (
    default_thread_tuning_profile as default_thread_tuning_profile,
)
from wgsextract_cli.core.samtools_commands import (
    get_sam_index_cmd as get_sam_index_cmd,
)
from wgsextract_cli.core.samtools_commands import (
    get_sam_sort_cmd as get_sam_sort_cmd,
)
from wgsextract_cli.core.samtools_commands import (
    get_sam_view_cmd as get_sam_view_cmd,
)

_process_group_kwargs = process_group_kwargs

ProcessStream: TypeAlias = int | IO[bytes] | IO[str] | None


class WGSExtractError(Exception):
    """Base exception for wgsextract-cli errors."""


def get_resource_defaults(
    threads_arg: int | str | None = None, memory_arg: str | None = None
) -> tuple[str, str]:
    """Calculate default CPU threads and memory if not provided."""
    return _get_resource_defaults(
        threads_arg, memory_arg, default_thread_tuning_profile
    )


def _normalize_subprocess_cmd(cmd: str | Sequence[object]) -> list[str]:
    """Expand shell-style command strings and configured Pixi tool wrappers."""
    from wgsextract_cli.core import (
        runtime,
        runtime_wrappers,
    )

    # Wrapper commands are built with each platform's quoting (list2cmdline on
    # Windows), so split them the same way; POSIX shlex drops Windows backslashes.
    from wgsextract_cli.core.dependencies import _tool_command_parts

    def split_wrapper_or_keep(value: str) -> list[str]:
        value = os.path.expanduser(value)
        if (
            runtime.is_wsl_tool_command(value)
            or runtime.is_bundled_tool_command(value)
            or runtime.is_pacman_tool_command(value)
        ):
            return [value]
        if os.path.exists(value):
            return [value]
        if "pixi run" in value or " " in value:
            return _tool_command_parts(value)
        return [value]

    if isinstance(cmd, str):
        cmd_list = [os.path.expanduser(part) for part in shlex.split(cmd)]
    else:
        cmd_list = []
        for index, item in enumerate(cmd):
            if index == 0 and isinstance(item, str):
                cmd_list.extend(split_wrapper_or_keep(item))
            else:
                cmd_list.append(
                    os.path.expanduser(str(item))
                    if isinstance(item, str)
                    else str(item)
                )

    if cmd_list and isinstance(cmd_list[0], str):
        executable = cmd_list[0]
        if runtime.is_wsl_tool_command(executable):
            return runtime_wrappers.wrap_command(cmd_list)

        if executable and " " in executable and not os.path.exists(executable):
            cmd_list = _tool_command_parts(executable) + cmd_list[1:]
            executable = cmd_list[0]

        if (
            executable
            and os.path.basename(executable) == executable
            and shutil.which(executable) is None
        ):
            from wgsextract_cli.core.dependencies import get_tool_path

            resolved = get_tool_path(executable)
            if resolved:
                if (
                    runtime.is_wsl_tool_command(resolved)
                    or runtime.is_bundled_tool_command(resolved)
                    or runtime.is_pacman_tool_command(resolved)
                ):
                    cmd_list = [resolved] + cmd_list[1:]
                elif os.path.exists(resolved):
                    cmd_list = [resolved] + cmd_list[1:]
                else:
                    cmd_list = _tool_command_parts(resolved) + cmd_list[1:]

    return runtime_wrappers.wrap_command(cmd_list)


def run_command(
    cmd: str | Sequence[object],
    capture_output: bool = False,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: IO[bytes] | IO[str] | int | None = None,
    stdout: IO[bytes] | IO[str] | int | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Helper to run subprocess with logging and registry."""
    cmd_list = _normalize_subprocess_cmd(cmd)

    cmd_str = " ".join(cmd_list)
    logging.debug(f"Running: {cmd_str}")

    proc_stdout = (
        stdout if stdout is not None else (subprocess.PIPE if capture_output else None)
    )
    proc_stderr = subprocess.PIPE if capture_output else None

    process = subprocess.Popen(
        cmd_list,
        stdout=proc_stdout,
        stderr=proc_stderr,
        stdin=stdin,
        text=True if capture_output or (stdout is None) else False,
        env=env,
        cwd=cwd,
        **process_group_kwargs(),
    )

    proc_registry.register_process(cmd_str, process)
    try:
        res_stdout, res_stderr = process.communicate()
        if check and process.returncode != 0:
            logging.error(f"Command failed: {cmd_str}")
            if res_stderr:
                logging.error(res_stderr)
            raise subprocess.CalledProcessError(
                process.returncode, cmd_list, res_stdout, res_stderr
            )
        return subprocess.CompletedProcess(
            cmd_list, process.returncode, res_stdout, res_stderr
        )
    finally:
        proc_registry.unregister_process(cmd_str, process)


@overload
def popen(
    cmd: str | Sequence[object],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: Literal[False] = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]: ...


@overload
def popen(
    cmd: str | Sequence[object],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: Literal[True] = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[str]: ...


def popen(
    cmd: str | Sequence[object],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
    """
    Start a registered subprocess without waiting for it.
    Default text=False to allow binary pipes (BAM/BCF).
    """
    cmd_list = _normalize_subprocess_cmd(cmd)

    cmd_str = " ".join(cmd_list)
    logging.debug(f"Popen: {cmd_str}")

    process = subprocess.Popen(
        cmd_list,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        text=text,
        env=env,
        **process_group_kwargs(),
    )
    proc_registry.register_process(cmd_str, process)

    def _unregister_finished_process() -> None:
        process.wait()
        proc_registry.unregister_process(cmd_str, process)

    threading.Thread(target=_unregister_finished_process, daemon=True).start()
    return process


def _stderr_tail(stream: IO[bytes], max_lines: int = 20) -> str:
    stream.seek(0)
    lines = stream.read().decode(errors="replace").strip().splitlines()
    return "\n".join(lines[-max_lines:])


def _is_sigpipe(returncode: int) -> bool:
    sigpipe = getattr(signal, "SIGPIPE", None)
    return sigpipe is not None and returncode in (-sigpipe, 128 + sigpipe)


def run_pipeline(
    stages: Sequence[str | Sequence[object]],
    *,
    stdin: ProcessStream = None,
    stdout: ProcessStream = None,
    env: Mapping[str, str] | None = None,
    description: str = "Pipeline",
) -> None:
    """
    Run commands connected stdout-to-stdin, wait for every stage, and raise
    WGSExtractError naming each stage that failed.

    Each stage's stderr is spooled to a temp file so a chatty stage cannot fill
    an unread pipe and deadlock the pipeline; a failing stage's stderr tail is
    included in the error. An upstream stage killed by SIGPIPE is not a failure
    when every later stage succeeded (the consumer intentionally stopped early).
    """
    if not stages:
        raise ValueError("run_pipeline requires at least one stage")

    procs: list[subprocess.Popen[bytes]] = []
    stderr_files: list[IO[bytes]] = []
    try:
        upstream: ProcessStream = stdin
        for index, stage in enumerate(stages):
            is_last = index == len(stages) - 1
            err_file: IO[bytes] = tempfile.TemporaryFile()
            stderr_files.append(err_file)
            proc = popen(
                stage,
                stdin=upstream,
                stdout=stdout if is_last else subprocess.PIPE,
                stderr=err_file,
                env=env,
            )
            # Close our copy of the previous stage's stdout so it sees SIGPIPE
            # if this stage exits early.
            if procs and procs[-1].stdout:
                procs[-1].stdout.close()
            procs.append(proc)
            upstream = proc.stdout

        returncodes = [proc.wait() for proc in procs]
        failures: list[str] = []
        for index, (stage, returncode) in enumerate(
            zip(stages, returncodes, strict=True)
        ):
            # Label from the caller's argv, not the resolved (possibly wrapped) one.
            args = (
                shlex.split(stage)
                if isinstance(stage, str)
                else [str(a) for a in stage]
            )
            stage_name = (
                " ".join([os.path.basename(args[0]), *args[1:2]]) if args else "?"
            )
            tail = _stderr_tail(stderr_files[index])
            downstream_ok = all(code == 0 for code in returncodes[index + 1 :])
            if returncode == 0 or (_is_sigpipe(returncode) and downstream_ok):
                if tail:
                    logging.debug(f"{stage_name} stderr:\n{tail}")
                continue
            detail = f"{stage_name} (exit {returncode})"
            if tail:
                detail += f":\n{tail}"
            failures.append(detail)
        if failures:
            raise WGSExtractError(f"{description} failed in " + "\n".join(failures))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        for err_file in stderr_files:
            err_file.close()
