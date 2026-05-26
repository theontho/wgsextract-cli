import logging
import os
import signal
import sys
import threading
import time
from types import FrameType
from typing import TextIO

from wgsextract_cli.core.utils import cleanup_processes

SIGNAL_EXPLANATIONS = {
    "SIGHUP": "terminal or parent session closed",
    "SIGINT": "interrupt from keyboard, usually Ctrl+C",
    "SIGQUIT": "quit from keyboard, usually Ctrl+\\",
    "SIGKILL": "force kill; cannot be caught or cleaned up",
    "SIGTERM": "normal termination request from kill or a process manager",
}


def describe_signal(signum: int) -> str:
    """Return a concise user-facing explanation for a signal number."""
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}: unknown signal"

    explanation = SIGNAL_EXPLANATIONS.get(signal_name, "process signal")
    return f"signal {signum} ({signal_name}: {explanation})"


def _parent_process_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False

    try:
        import psutil

        return bool(psutil.pid_exists(parent_pid))
    except ImportError:
        pass

    if sys.platform == "win32":
        return True

    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class EmojiFormatter(logging.Formatter):
    """Custom formatter to add emojis to log levels."""

    LEVEL_EMOJIS = {
        logging.DEBUG: "🔍",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❌",
        logging.CRITICAL: "🚨",
    }

    def format(self, record: logging.LogRecord) -> str:
        level_fmt = self.LEVEL_EMOJIS.get(record.levelno, record.levelname)
        record.levelname = level_fmt
        return super().format(record)


def configure_stdio_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        _reconfigure_stream(stream)


def _reconfigure_stream(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(errors="replace")
    except (OSError, ValueError):
        logging.debug("Could not reconfigure stdio encoding.", exc_info=True)


def configure_logging(*, debug: bool = False, quiet: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        EmojiFormatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    elif quiet:
        logging.getLogger().setLevel(logging.ERROR)


def install_signal_handlers() -> None:
    def signal_handler(signum: int, frame: FrameType | None) -> None:
        logging.info(f"Received {describe_signal(signum)}, cleaning up...")
        cleanup_processes()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def start_parent_monitor(parent_pid: int | None) -> None:
    if not parent_pid:
        return

    def monitor_parent() -> None:
        while True:
            if not _parent_process_is_alive(parent_pid):
                logging.warning(f"Parent process {parent_pid} disappeared, exiting...")
                cleanup_processes()
                os._exit(1)
            time.sleep(2)

    monitor_thread = threading.Thread(target=monitor_parent, daemon=True)
    monitor_thread.start()
