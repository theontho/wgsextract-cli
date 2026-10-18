import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any


class ProcessRegistry:
    """Tracks running subprocesses so they can be terminated on exit."""

    def __init__(self) -> None:
        self.processes: dict[str, set[subprocess.Popen[Any]]] = {}
        # Reentrant: signal handlers run cleanup() on the main thread, possibly
        # while that thread is already inside register/unregister.
        self._lock = threading.RLock()

    def register_process(self, key: str, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self.processes.setdefault(key, set()).add(process)

    def unregister_process(self, key: str, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            processes = self.processes.get(key)
            if not processes:
                return
            processes.discard(process)
            if not processes:
                del self.processes[key]

    def cleanup(self) -> None:
        """Terminate all registered processes (SIGTERM, then SIGKILL)."""
        with self._lock:
            all_processes = [
                proc for processes in self.processes.values() for proc in processes
            ]
            self.processes.clear()
        if not all_processes:
            return

        for proc in all_processes:
            if proc.poll() is None:
                try:
                    if sys.platform == "win32":
                        proc.send_signal(
                            getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                        )
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except OSError:
                    logging.debug(
                        "Process %s could not be terminated.", proc.pid, exc_info=True
                    )

        time.sleep(0.5)

        for proc in all_processes:
            if proc.poll() is None:
                try:
                    if sys.platform == "win32":
                        proc.kill()
                    else:
                        os.killpg(os.getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
                except OSError:
                    logging.debug(
                        "Process %s could not be killed.", proc.pid, exc_info=True
                    )


proc_registry = ProcessRegistry()


def cleanup_processes() -> None:
    """Entry point for atexit and signal handlers."""
    proc_registry.cleanup()


atexit.register(cleanup_processes)


def process_group_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}
