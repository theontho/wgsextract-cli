import atexit
import logging
import os
import platform
import shutil
import subprocess

from wgsextract_cli.core.runtime import default_thread_tuning_profile

try:
    import psutil
except ImportError:
    psutil = None

import signal
import sys
import threading
import time


class WGSExtractError(Exception):
    """Base exception for wgsextract-cli errors."""

    pass


class ProcessRegistry:
    """
    Central registry for tracking sub-processes and cancel events.
    Enables reliable cleanup on application exit.
    """

    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register_process(self, key: str, process: subprocess.Popen):
        with self._lock:
            self.processes[key] = process

    def unregister_process(self, key: str):
        with self._lock:
            if key in self.processes:
                del self.processes[key]

    def register_event(self, key: str, event: threading.Event):
        with self._lock:
            self.events[key] = event

    def unregister_event(self, key: str):
        with self._lock:
            if key in self.events:
                del self.events[key]

    def cleanup(self):
        """Terminate all registered processes and set all events."""
        with self._lock:
            # 1. Signal all events
            for event in self.events.values():
                event.set()

            # 2. Terminate all processes
            if not self.processes:
                return

            # Send termination signals
            for _key, proc in self.processes.items():
                if proc.poll() is None:
                    try:
                        if sys.platform == "win32":
                            # Windows: send CTRL_BREAK_EVENT to process group
                            # Use getattr to avoid MyPy error on non-Windows
                            proc.send_signal(
                                getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                            )
                        else:
                            # Unix: kill the process group
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        pass

            # Brief wait for graceful exit
            time.sleep(0.5)

            # Force kill any still alive
            for _key, proc in self.processes.items():
                if proc.poll() is None:
                    try:
                        if sys.platform == "win32":
                            proc.kill()
                        else:
                            os.killpg(
                                os.getpgid(proc.pid), getattr(signal, "SIGKILL", 9)
                            )
                    except Exception:
                        pass

            self.processes.clear()


proc_registry = ProcessRegistry()


def cleanup_processes():
    """Entry point for atexit and signal handlers."""
    proc_registry.cleanup()


atexit.register(cleanup_processes)


def _process_group_kwargs():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def get_resource_defaults(threads_arg=None, memory_arg=None):
    """
    Calculate default CPU threads and memory if not provided.
    Logic inspired by Adjust_Mem in program/mainwindow.py.
    """
    # 1. Threads
    if threads_arg is not None:
        threads = str(threads_arg)
    else:
        threads = str(default_thread_tuning_profile().threads)

    # 2. Memory (in GB per thread for samtools sort)
    if memory_arg is not None:
        memory = str(memory_arg)
    else:
        # Default to 4GB total or 1GB per thread, whichever is smaller
        if psutil:
            total_mem_gb = psutil.virtual_memory().total / (1024**3)
            # Use 25% of system memory
            safe_mem = max(2, int(total_mem_gb * 0.25))
            # samtools sort -m is PER THREAD
            mem_per_thread = max(1, safe_mem // int(threads))
            memory = f"{mem_per_thread}G"
        else:
            memory = "1G"

    return threads, memory


def get_sam_sort_cmd(
    out_file,
    threads,
    memory,
    fmt="BAM",
    reference=None,
    name_sort=False,
    temp_dir=None,
):
    """
    Returns a command list for sorting BAM/CRAM.
    Uses sambamba if available (except on macOS) and format is BAM, else samtools.
    """
    threads_val = int(threads)
    # Convert memory (e.g. "1G") to just "1" for calculation
    mem_val = int(memory.rstrip("GgMm"))
    is_gb = memory.lower().endswith("g")

    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and fmt == "BAM" and not is_macos:
        # sambamba -m is TOTAL memory
        total_mem = mem_val * threads_val
        total_mem_str = f"{total_mem}G" if is_gb else f"{total_mem}M"
        cmd = [
            "sambamba",
            "sort",
            "-t",
            threads,
            "-m",
            total_mem_str,
            "-o",
            out_file,
            "/dev/stdin",
        ]
        if name_sort:
            cmd.insert(2, "-n")
        if temp_dir:
            cmd.insert(2, "--tmpdir")
            cmd.insert(3, temp_dir)
        return cmd
    else:
        # samtools sort -m is PER THREAD
        cmd = ["samtools", "sort", "-@", threads, "-m", memory, "-o", out_file]
        if name_sort:
            cmd.append("-n")
        if temp_dir:
            cmd += ["-T", temp_dir]
        if fmt == "CRAM":
            cmd += ["-O", "CRAM"]
            if reference:
                cmd += ["--reference", reference]
        elif fmt == "SAM":
            cmd += ["-O", "SAM"]
        else:
            cmd += ["-O", "BAM"]
        return cmd


def get_sam_index_cmd(file_path, threads="1"):
    """
    Returns a command list for indexing BAM/CRAM.
    Uses sambamba if available (except on macOS) and file is BAM, else samtools.
    """

    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and file_path.lower().endswith(".bam") and not is_macos:
        return ["sambamba", "index", "-t", threads, file_path]
    else:
        return ["samtools", "index", file_path]


def get_sam_view_cmd(threads="1", fmt="BAM", reference=None, is_input_sam=False):
    """
    Returns a command list for viewing/converting BAM/CRAM.
    Uses sambamba if available (except on macOS) and fmt is BAM, else samtools.
    """

    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and fmt == "BAM" and not reference and not is_macos:
        cmd = ["sambamba", "view", "-t", threads, "-f", "bam"]
        if is_input_sam:
            cmd += ["-S"]
        return cmd
    else:
        cmd = ["samtools", "view", "-@", threads]
        if fmt == "CRAM":
            cmd += ["-O", "CRAM"]
            if reference:
                cmd += ["-T", reference]
        elif fmt == "BAM":
            cmd += ["-b"]

        return cmd


def _normalize_subprocess_cmd(cmd):
    """Expand shell-style command strings and configured Pixi tool wrappers."""
    import shlex

    from wgsextract_cli.core import (
        runtime,
        runtime_wrappers,
    )

    def split_wrapper_or_keep(value: str) -> list[str]:
        if (
            runtime.is_wsl_tool_command(value)
            or runtime.is_bundled_tool_command(value)
            or runtime.is_pacman_tool_command(value)
        ):
            return [value]
        if os.path.exists(value):
            return [value]
        if "pixi run" in value or " " in value:
            return shlex.split(value)
        return [value]

    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd)
    else:
        cmd_list = []
        for item in cmd:
            if isinstance(item, str) and item == cmd[0]:
                cmd_list.extend(split_wrapper_or_keep(item))
            else:
                cmd_list.append(item)

    if cmd_list and isinstance(cmd_list[0], str):
        executable = cmd_list[0]
        if runtime.is_wsl_tool_command(executable):
            return runtime_wrappers.wrap_command(cmd_list)

        if executable and " " in executable and not os.path.exists(executable):
            cmd_list = shlex.split(executable) + cmd_list[1:]
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
                    cmd_list = shlex.split(resolved) + cmd_list[1:]

    return runtime_wrappers.wrap_command(cmd_list)


def run_command(
    cmd, capture_output=False, check=True, env=None, stdin=None, stdout=None
):
    """Helper to run subprocess with logging and registry."""
    cmd_list = _normalize_subprocess_cmd(cmd)

    cmd_str = " ".join(cmd_list)
    logging.debug(f"Running: {cmd_str}")

    # If stdout/stdin are provided, they take precedence over capture_output
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
        **_process_group_kwargs(),
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
            cmd, process.returncode, res_stdout, res_stderr
        )
    finally:
        proc_registry.unregister_process(cmd_str)
