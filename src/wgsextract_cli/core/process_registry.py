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
    """
    Central registry for tracking sub-processes and cancel events.
    Enables reliable cleanup on application exit.
    """

    def __init__(self) -> None:
        self.processes: dict[str, subprocess.Popen[Any]] = {}
        self.events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register_process(self, key: str, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self.processes[key] = process

    def unregister_process(self, key: str) -> None:
        with self._lock:
            if key in self.processes:
                del self.processes[key]

    def register_event(self, key: str, event: threading.Event) -> None:
        with self._lock:
            self.events[key] = event

    def unregister_event(self, key: str) -> None:
        with self._lock:
            if key in self.events:
                del self.events[key]

    def cleanup(self) -> None:
        """Terminate all registered processes and set all events."""
        with self._lock:
            for event in self.events.values():
                event.set()

            if not self.processes:
                return

            for _key, proc in self.processes.items():
                if proc.poll() is None:
                    try:
                        if sys.platform == "win32":
                            proc.send_signal(
                                getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                            )
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        logging.debug(
                            "Process %s could not be terminated.",
                            proc.pid,
                            exc_info=True,
                        )

            time.sleep(0.5)

            for _key, proc in self.processes.items():
                if proc.poll() is None:
                    try:
                        if sys.platform == "win32":
                            proc.kill()
                        else:
                            os.killpg(
                                os.getpgid(proc.pid), getattr(signal, "SIGKILL", 9)
                            )
                    except (OSError, ProcessLookupError):
                        logging.debug(
                            "Process %s could not be killed.", proc.pid, exc_info=True
                        )

            self.processes.clear()


proc_registry = ProcessRegistry()


def cleanup_processes() -> None:
    """Entry point for atexit and signal handlers."""
    proc_registry.cleanup()


atexit.register(cleanup_processes)


def process_group_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}
