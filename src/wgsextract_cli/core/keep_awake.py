"""Best-effort idle/sleep inhibition for an explicitly enabled command."""

import ctypes
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from typing import cast

from .process_registry import proc_registry, process_group_kwargs


def _windows_execution_state() -> Callable[[int], int]:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("WinDLL is unavailable.")
    kernel32 = loader("kernel32", use_last_error=True)
    set_state = kernel32.SetThreadExecutionState
    set_state.argtypes = [ctypes.c_uint]
    set_state.restype = ctypes.c_uint
    return cast(Callable[[int], int], set_state)


@contextmanager
def keep_awake(enabled: bool = False) -> Iterator[None]:
    """Hold only during this command; failures never mask command exceptions."""
    process: subprocess.Popen[bytes] | None = None
    restore: Callable[[], object] | None = None
    key = "wgsextract keep-awake"
    if enabled:
        try:
            if sys.platform == "win32":
                set_state = _windows_execution_state()
                previous = set_state(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                if previous:
                    restore = partial(set_state, previous)
                else:
                    logging.debug("Windows rejected the keep-awake request.")
            else:
                command: list[str] = []
                if sys.platform == "darwin" and (tool := shutil.which("caffeinate")):
                    command = [tool, "-i", "-w", str(os.getpid())]
                elif sys.platform.startswith("linux") and (
                    tool := shutil.which("systemd-inhibit")
                ):
                    command = [
                        tool,
                        "--what=idle:sleep",
                        "--who=wgsextract",
                        "--why=WGS Extract command is running",
                        "--mode=block",
                        sys.executable,
                        "-c",
                        "import sys; sys.stdin.buffer.read()",
                    ]
                if command:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **process_group_kwargs(),
                    )
                    proc_registry.register_process(key, process)
                else:
                    logging.debug("No supported keep-awake helper is available.")
        except Exception:
            logging.debug("Keep-awake setup is unavailable.", exc_info=True)
    try:
        yield
    finally:
        try:
            if restore:
                restore()
            if process:
                if process.stdin:
                    with suppress(OSError):
                        process.stdin.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        except Exception:
            logging.debug("Keep-awake cleanup failed.", exc_info=True)
        finally:
            if process:
                proc_registry.unregister_process(key, process)
