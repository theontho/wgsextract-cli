import codecs
import contextlib
import functools
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from os import PathLike
from pathlib import Path
from typing import IO, Concatenate, Literal, ParamSpec, TypeAlias, TypeVar, overload

from wgsextract_cli.core import live_status
from wgsextract_cli.core.download_progress import CancelEvent, redact_urls
from wgsextract_cli.core.paths import PathInput, command_is_path, expand_path
from wgsextract_cli.core.process_registry import (
    cleanup_processes as cleanup_processes,
)
from wgsextract_cli.core.process_registry import (
    proc_registry,
    process_group_kwargs,
    terminate_trees,
)
from wgsextract_cli.core.progress import Heartbeat
from wgsextract_cli.core.resource_policy import (
    get_resource_defaults as _get_resource_defaults,
)
from wgsextract_cli.core.run_log import active_run_log, record_command
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

# Set while a library (wgsextract_cli.api) call runs, so tools that would
# inherit stdout write to stderr instead and the caller's stdout stays clean.
# A process-wide flag rather than a contextvar because commands start tools
# from worker threads, which do not inherit context; api calls are serialised.
_children_stdout_to_stderr = False


@contextlib.contextmanager
def child_stdout_to_stderr() -> Iterator[None]:
    """Send the stdout of tools started without an explicit stdout to stderr."""
    global _children_stdout_to_stderr
    previous = _children_stdout_to_stderr
    _children_stdout_to_stderr = True
    try:
        yield
    finally:
        _children_stdout_to_stderr = previous


def _child_stdout(stdout: ProcessStream) -> ProcessStream:
    """The stdout to give a tool: ``stdout`` itself unless it would inherit
    ours while ``child_stdout_to_stderr`` is active."""
    if stdout is not None or not _children_stdout_to_stderr:
        return stdout
    try:
        if sys.stderr is not None:
            sys.stderr.flush()
            return sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        os.fstat(2)
        return 2
    except OSError:
        return subprocess.DEVNULL


class WGSExtractError(Exception):
    """Base exception for wgsextract-cli errors (exit code 1)."""


class UsageError(WGSExtractError):
    """The command was invoked wrongly, e.g. a required option is missing (exit code 2)."""


class MissingDependencyError(WGSExtractError):
    """A required tool, JAR or environment is missing or doesn't run (exit code 3)."""


class ToolExecutionError(WGSExtractError, subprocess.CalledProcessError):
    """Keep subprocess exception attributes for callers' existing recovery paths."""

    def __str__(self) -> str:
        output = self.stderr or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        tail = "\n".join(redact_urls(str(output)).strip().splitlines()[-8:])[-2000:]
        tool = Path(str(self.cmd[0])).name if self.cmd else "Tool"
        return f"{tool} failed (exit {self.returncode}): {tail or 'no error output'}"


class ToolLaunchError(MissingDependencyError, OSError):
    """A clean dependency error that remains catchable as an OS failure."""


def get_resource_defaults(
    threads_arg: int | str | None = None, memory_arg: str | None = None
) -> tuple[str, str]:
    """Calculate default CPU threads and memory if not provided."""
    return _get_resource_defaults(
        threads_arg, memory_arg, default_thread_tuning_profile
    )


def _normalize_subprocess_cmd(cmd: PathInput | Sequence[object]) -> list[str]:
    """Expand shell-style command strings and configured Pixi tool wrappers."""
    # Wrapper commands are built with each platform's quoting (list2cmdline on
    # Windows), so split them the same way; POSIX shlex drops Windows backslashes.
    from wgsextract_cli.core.tool_commands import _tool_command_parts

    def split_wrapper_or_keep(value: str) -> list[str]:
        value = expand_path(value)
        if command_is_path(value):
            return [value]
        if "pixi run" in value or " " in value:
            return _tool_command_parts(value)
        return [value]

    literal_executable = isinstance(cmd, PathLike)
    if isinstance(cmd, PathLike):
        cmd_list = [expand_path(cmd)]
    elif isinstance(cmd, str):
        cmd_list = [expand_path(part) for part in shlex.split(cmd)]
    else:
        cmd_list = []
        for index, item in enumerate(cmd):
            if index == 0 and isinstance(item, PathLike):
                literal_executable = True
                cmd_list.append(expand_path(item))
            elif index == 0 and isinstance(item, str):
                cmd_list.extend(split_wrapper_or_keep(item))
            else:
                cmd_list.append(
                    expand_path(item)
                    if isinstance(item, (str, PathLike))
                    else str(item)
                )

    if cmd_list and isinstance(cmd_list[0], str):
        executable = cmd_list[0]
        if (
            not literal_executable
            and executable
            and " " in executable
            and not command_is_path(executable)
        ):
            cmd_list = _tool_command_parts(executable) + cmd_list[1:]
            executable = cmd_list[0]

        if (
            executable
            and Path(executable).name == executable
            and shutil.which(executable) is None
        ):
            from wgsextract_cli.core.dependencies import get_tool_path

            resolved = get_tool_path(executable)
            if resolved:
                if command_is_path(resolved):
                    cmd_list = [resolved] + cmd_list[1:]
                else:
                    cmd_list = _tool_command_parts(resolved) + cmd_list[1:]

    if "--java-options" in cmd_list:
        from wgsextract_cli.core.pixi_tools import windows_gatk_java_options

        try:
            cmd_list = windows_gatk_java_options(cmd_list)
        except ValueError as e:
            raise WGSExtractError(str(e)) from e
    if cmd_list:
        from wgsextract_cli.core.windows_arm64 import (
            assert_native_windows_command,
            unsupported_windows_arm64_tool,
        )

        executable_name = cmd_list[0].replace("\\", "/").rsplit("/", 1)[-1]
        tool = executable_name.lower().removesuffix(".exe")
        if "org.broadinstitute.hellbender.Main" in cmd_list:
            tool = "gatk"
        if reason := unsupported_windows_arm64_tool(tool):
            raise WGSExtractError(reason)
        try:
            assert_native_windows_command(cmd_list)
        except (OSError, ValueError) as error:
            raise WGSExtractError(str(error)) from error
    return cmd_list


_P = ParamSpec("_P")
_R = TypeVar("_R")
_SUBCOMMAND = re.compile(r"[a-z][a-z0-9_-]*")


def _caller_args(cmd: PathInput | Sequence[object]) -> list[str]:
    """cmd's words as the caller wrote them, before any wrapping."""
    if isinstance(cmd, PathLike):
        return [expand_path(cmd)]
    if isinstance(cmd, str):
        return shlex.split(cmd)
    return [str(a) for a in cmd]


def step_label(args: Sequence[str]) -> str:
    """'samtools view' for a tool and its subcommand, else the tool's name."""
    if not args:
        return "?"
    words = [Path(args[0]).stem]
    if len(args) > 1 and _SUBCOMMAND.fullmatch(args[1]):
        words.append(args[1])
    return " ".join(words)


def _shows_step(
    func: Callable[Concatenate[PathInput | Sequence[object], _P], _R],
) -> Callable[Concatenate[PathInput | Sequence[object], _P], _R]:
    """Name the tool a function runs on the terminal's progress line."""

    @functools.wraps(func)
    def wrapper(
        cmd: PathInput | Sequence[object], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> _R:
        with live_status.step(step_label(_caller_args(cmd))):
            return func(cmd, *args, **kwargs)

    return wrapper


def _tee_stderr(
    fd: int,
    tail: bytearray,
    target: IO[str] | None,
    redact_output_urls: bool = False,
    sources: Sequence[str] = (),
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    binary = getattr(target, "buffer", None)
    pending = ""
    with os.fdopen(fd, "rb") as stream:
        while chunk := stream.read1(65536):
            tail.extend(chunk)
            del tail[:-65536]
            if target is None:  # no stderr (a windowed GUI): keep the tail only
                continue
            if redact_output_urls:
                pending += decoder.decode(chunk)
                lines = pending.splitlines(keepends=True)
                pending = ""
                for line in lines:
                    if line.endswith(("\n", "\r")):
                        target.write(redact_urls(line, sources=sources))
                        target.flush()
                    else:
                        pending = line
                continue
            if binary is not None:
                binary.write(chunk)
                binary.flush()
            else:
                target.write(decoder.decode(chunk))
                target.flush()
        if target is None:
            return
        if redact_output_urls:
            target.write(
                redact_urls(pending + decoder.decode(b"", final=True), sources=sources)
            )
            target.flush()
        elif binary is None:
            target.write(decoder.decode(b"", final=True))


_Relay = tuple[int, int, str]


def _relay_if_terminal(
    stream: ProcessStream, inherited_fd: int, target: str, relays: list[_Relay]
) -> ProcessStream:
    """A pipe instead of ``stream`` when the child would write straight to the
    terminal under the progress line; _start_relays copies it to sys.<target>,
    which clears the line first."""
    if not live_status.terminal_bound(stream, inherited_fd):
        return stream
    read_fd, write_fd = os.pipe()
    relays.append((read_fd, write_fd, target))
    return write_fd


def _start_relays(relays: list[_Relay], launched: bool) -> list[threading.Thread]:
    threads = []
    for read_fd, write_fd, target in relays:
        os.close(write_fd)
        if not launched:
            os.close(read_fd)
            continue
        thread = threading.Thread(
            target=_tee_stderr,
            args=(read_fd, bytearray(), getattr(sys, target)),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


@_shows_step
def run_command(
    cmd: PathInput | Sequence[object],
    capture_output: bool = False,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: IO[bytes] | IO[str] | int | None = None,
    stdout: IO[bytes] | IO[str] | int | None = None,
    cwd: PathInput | None = None,
    *,
    redact_output_urls: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Helper to run subprocess with logging and registry."""
    cmd_list = _normalize_subprocess_cmd(cmd)
    display_cmd = [redact_urls(part) for part in cmd_list]
    record_command(display_cmd)

    cmd_str = " ".join(display_cmd)
    logging.debug(f"Running: {cmd_str}")

    proc_stdout = (
        stdout
        if stdout is not None
        else (subprocess.PIPE if capture_output else _child_stdout(None))
    )
    stderr_read, stderr_write = (
        os.pipe() if check and not capture_output else (None, None)
    )
    stderr_tail = bytearray()
    stderr_thread = None
    relays: list[_Relay] = []
    proc_stdout = _relay_if_terminal(proc_stdout, 1, "stdout", relays)
    proc_stderr = _relay_if_terminal(
        subprocess.PIPE if capture_output else stderr_write, 2, "stderr", relays
    )

    text_mode = capture_output or stdout is None
    try:
        process = subprocess.Popen(
            cmd_list,
            stdout=proc_stdout,
            stderr=proc_stderr,
            stdin=stdin,
            text=text_mode,
            errors="replace" if text_mode else None,
            env=env,
            cwd=cwd,
            **process_group_kwargs(),
        )
    except OSError as error:
        for fd in (stderr_read, stderr_write):
            if fd is not None:
                os.close(fd)
        _start_relays(relays, launched=False)
        raise ToolLaunchError(
            f"Could not start {Path(cmd_list[0]).name}: {error.strerror or error}. "
            "Run 'wgsextract deps check' to validate installed tools."
        ) from error

    relay_threads = _start_relays(relays, launched=True)
    if stderr_read is not None:
        assert stderr_write is not None
        os.close(stderr_write)
        stderr_thread = threading.Thread(
            target=_tee_stderr,
            args=(stderr_read, stderr_tail, sys.stderr, redact_output_urls, cmd_list),
            daemon=True,
        )
        stderr_thread.start()
    proc_registry.register_process(cmd_str, process)
    try:
        if sys.platform == "win32":
            # Infinite Windows waits defer Python's main-thread signal handlers.
            while True:
                try:
                    res_stdout, res_stderr = process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
        else:
            res_stdout, res_stderr = process.communicate()
        for thread in [stderr_thread, *relay_threads]:
            if thread is None:
                continue
            if sys.platform == "win32":
                while thread.is_alive():
                    thread.join(timeout=0.5)
            else:
                thread.join()
        if check and process.returncode != 0:
            if stderr_read is not None:
                res_stderr = "\n".join(
                    stderr_tail.decode(errors="replace").splitlines()[-20:]
                )
            raise ToolExecutionError(
                process.returncode,
                display_cmd,
                res_stdout,
                redact_urls(res_stderr, sources=cmd_list)
                if isinstance(res_stderr, str)
                else res_stderr,
            )
        return subprocess.CompletedProcess(
            cmd_list, process.returncode, res_stdout, res_stderr
        )
    except KeyboardInterrupt:
        if sys.platform == "win32":
            cleanup_processes()
        else:
            # The tool runs in its own session, so Ctrl-C did not reach it.
            terminate_trees([process])
        raise
    finally:
        proc_registry.unregister_process(cmd_str, process)
        if stderr_thread:
            stderr_thread.join(timeout=0.1)


@overload
def popen(
    cmd: PathInput | Sequence[object],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: Literal[False] = False,
    env: Mapping[str, str] | None = None,
    cwd: PathInput | None = None,
) -> subprocess.Popen[bytes]: ...


@overload
def popen(
    cmd: PathInput | Sequence[object],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: Literal[True] = True,
    env: Mapping[str, str] | None = None,
    cwd: PathInput | None = None,
) -> subprocess.Popen[str]: ...


def popen(
    cmd: PathInput | Sequence[object],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: bool = False,
    env: Mapping[str, str] | None = None,
    cwd: PathInput | None = None,
) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
    """
    Start a registered subprocess without waiting for it.
    Default text=False to allow binary pipes (BAM/BCF).
    """
    cmd_list = _normalize_subprocess_cmd(cmd)
    display_cmd = [redact_urls(part) for part in cmd_list]
    record_command(display_cmd)

    cmd_str = " ".join(display_cmd)
    logging.debug(f"Popen: {cmd_str}")

    relays: list[_Relay] = []
    try:
        process = subprocess.Popen(
            cmd_list,
            stdout=_relay_if_terminal(_child_stdout(stdout), 1, "stdout", relays),
            stderr=_relay_if_terminal(stderr, 2, "stderr", relays),
            stdin=stdin,
            text=text,
            errors="replace" if text else None,
            env=env,
            cwd=cwd,
            **process_group_kwargs(),
        )
    except OSError as error:
        _start_relays(relays, launched=False)
        raise ToolLaunchError(
            f"Could not start {Path(cmd_list[0]).name}: {error.strerror or error}. "
            "Run 'wgsextract deps check' to validate installed tools."
        ) from error
    _start_relays(relays, launched=True)
    proc_registry.register_process(cmd_str, process)

    def _unregister_finished_process() -> None:
        process.wait()
        proc_registry.unregister_process(cmd_str, process)

    threading.Thread(target=_unregister_finished_process, daemon=True).start()
    return process


def wait_with_cancel(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    cancel_event: CancelEvent | None = None,
) -> bool:
    """Waits for a process while checking for a cancel event."""
    while process.poll() is None:
        if cancel_event and cancel_event.is_set():
            logging.info(f"Terminating process {process.pid} due to cancellation.")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            return False
        time.sleep(0.1)
    return process.returncode == 0


def _stderr_tail(stream: IO[bytes], max_lines: int = 20) -> str:
    stream.seek(0)
    lines = stream.read().decode(errors="replace").strip().splitlines()
    return "\n".join(lines[-max_lines:])


def _is_sigpipe(returncode: int) -> bool:
    sigpipe = getattr(signal, "SIGPIPE", None)
    return sigpipe is not None and returncode in (-sigpipe, 128 + sigpipe)


def run_pipeline(
    stages: Sequence[PathInput | Sequence[object]],
    *,
    stdin: ProcessStream = None,
    stdout: ProcessStream = None,
    env: Mapping[str, str] | None = None,
    description: str = "Pipeline",
    cancel_event: CancelEvent | None = None,
    stderr_dir: PathInput | None = None,
    heartbeat: bool = False,
    expected_reads: int | None = None,
) -> None:
    """
    Run commands connected stdout-to-stdin, wait for every stage, and raise
    WGSExtractError naming each stage that failed.

    Each stage's stderr is spooled to a temp file so a chatty stage cannot fill
    an unread pipe and deadlock the pipeline; a failing stage's stderr tail is
    included in the error. An upstream stage killed by SIGPIPE is not a failure
    when every later stage succeeded (the consumer intentionally stopped early).
    Cancellation kills and reaps every started stage before closing its files.
    """
    if not stages:
        raise ValueError("run_pipeline requires at least one stage")

    procs: list[subprocess.Popen[bytes]] = []
    pipeline_step = contextlib.ExitStack()
    stderr_files: list[IO[bytes]] = []
    readers: list[tuple[IO[bytes], bool]] = []
    partials: list[bytes] = []
    monitor: Heartbeat | None = None
    names: list[str] = []
    progress_args = (
        [_normalize_subprocess_cmd(stage) for stage in stages]
        if heartbeat or active_run_log()
        else []
    )

    def is_aligner(args: Sequence[str]) -> bool:
        return any(Path(arg).stem in {"bwa", "bwa-mem2", "minimap2"} for arg in args)

    has_aligner = any(is_aligner(args) for args in progress_args)

    def scan() -> None:
        for index, (reader, aligner) in enumerate(readers):
            while chunk := reader.read(65536):
                logging.debug(
                    "%s stderr: %s", names[index], chunk.decode(errors="replace")
                )
                if aligner and monitor:
                    data = partials[index] + chunk
                    lines = data.split(b"\n")
                    partials[index] = lines.pop()[-65536:]
                    for line in lines:
                        monitor.read_line(line.decode(errors="replace"))

    try:
        upstream: ProcessStream = stdin
        for index, stage in enumerate(stages):
            if cancel_event and cancel_event.is_set():
                raise WGSExtractError(f"{description} cancelled")
            is_last = index == len(stages) - 1
            err_file: IO[bytes]
            if heartbeat or active_run_log():
                err_file = (
                    tempfile.NamedTemporaryFile(delete=False, dir=stderr_dir)
                    if stderr_dir is not None
                    else tempfile.NamedTemporaryFile(delete=False)
                )
            else:
                err_file = (
                    tempfile.TemporaryFile(dir=stderr_dir)
                    if stderr_dir is not None
                    else tempfile.TemporaryFile()
                )
            stderr_files.append(err_file)
            if heartbeat or active_run_log():
                args = progress_args[index]
                name = Path(args[0]).stem if args else "?"
                names.append(name)
                readers.append(
                    (
                        open(err_file.name, "rb"),
                        (
                            not has_aligner
                            and any(
                                Path(arg).stem == "samtools"
                                and args[position + 1] == "fastq"
                                for position, arg in enumerate(args[:-1])
                            )
                        )
                        or is_aligner(args),
                    )
                )
                partials.append(b"")
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

        pipeline_step.enter_context(
            live_status.step(
                " | ".join(step_label(_caller_args(stage)) for stage in stages)
            )
        )
        if heartbeat or active_run_log():
            monitor = Heartbeat(description, expected_reads, scan=scan, emit=heartbeat)
            monitor.__enter__()
        returncodes = []
        for proc in procs:
            while cancel_event is not None and proc.poll() is None:
                if cancel_event.is_set():
                    raise WGSExtractError(f"{description} cancelled")
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            returncodes.append(proc.wait())
        if monitor:
            monitor.__exit__()
            monitor = None
        failures: list[str] = []
        for index, (stage, returncode) in enumerate(
            zip(stages, returncodes, strict=True)
        ):
            # Label from the caller's argv, not the resolved (possibly wrapped) one.
            args = _caller_args(stage)
            stage_name = " ".join([Path(args[0]).name, *args[1:2]]) if args else "?"
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
        if monitor:
            monitor.__exit__()
        pipeline_step.close()
        for reader, _ in readers:
            reader.close()
        for err_file in stderr_files:
            name = err_file.name
            err_file.close()
            if isinstance(name, str):
                Path(name).unlink(missing_ok=True)
