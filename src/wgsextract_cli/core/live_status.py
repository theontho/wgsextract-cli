"""A one-line progress display for long-running commands on a terminal.

While a command runs, the bottom line of the terminal shows what it is doing
and for how long, for example::

    / microarray: samtools mpileup, 4m 12s

It is redrawn in place with a carriage return (no escape codes, so it works
in any console, Windows included) and only on a terminal: dispatch starts it
when stderr is a TTY and the run is neither --json nor --quiet, so piped,
logged and scripted output never contains it. Everything else written to the
terminal clears the line first, and it comes back once that output ends in a
newline.

Code that runs a step (run_command, run_pipeline) names it with ``step()``;
progress counts (core/progress.Heartbeat) go in ``detail()``. Both do nothing
when no display is active.
"""

import contextlib
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from typing import IO, Any

_SPINNER = "-\\|/"
_active: "LiveStatus | None" = None


def wanted(stream: IO[str] | None, *, json: bool, quiet: bool) -> bool:
    """Whether a run with these settings may show the display on stream."""
    if stream is None or json or quiet or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _elapsed(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


class _Clearing:
    """A stream that clears the status line before anything is written to it.

    Wraps a text stream, and its binary .buffer the same way."""

    def __init__(self, status: "LiveStatus", stream: Any) -> None:
        self._status, self._stream = status, stream

    def write(self, data: Any) -> int:
        with self._status.lock:
            self._status.clear_locked()
            written: int = self._stream.write(data)
            if data:
                newline = "\n" if isinstance(data, str) else b"\n"
                self._status.line_open = not data.endswith(newline)
            return written

    def flush(self) -> None:
        self._stream.flush()

    @property
    def buffer(self) -> "_Clearing":
        return _Clearing(self._status, self._stream.buffer)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class LiveStatus:
    def __init__(
        self,
        label: str,
        stream: IO[str],
        *,
        delay: float = 1.0,
        interval: float = 0.25,
    ) -> None:
        self.label, self.stream = label, stream
        self.delay, self.interval = delay, interval
        self.lock = threading.RLock()
        self.steps: list[str] = []
        self.detail = ""
        self.line_open = False
        self._drawn = 0
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._saved: list[tuple[Any, str, Any]] = []

    def text(self) -> str:
        frame = _SPINNER[int((time.monotonic() - self._started) * 4) % len(_SPINNER)]
        parts = [self.label]
        if self.steps:
            parts[0] += f": {self.steps[-1]}"
        parts.append(_elapsed(time.monotonic() - self._started))
        if self.detail:
            parts.append(self.detail)
        width = shutil.get_terminal_size((80, 24)).columns - 1
        return f"{frame} {', '.join(parts)}"[:width]

    def draw(self) -> None:
        with self.lock:
            if self.line_open:
                return
            text = self.text()
            self.stream.write("\r" + text.ljust(self._drawn) + "\r")
            self.stream.flush()
            self._drawn = len(text)

    def clear_locked(self) -> None:
        if self._drawn:
            self.stream.write("\r" + " " * self._drawn + "\r")
            self.stream.flush()
            self._drawn = 0

    def _run(self) -> None:
        if self._stop.wait(self.delay):
            return
        while not self._stop.is_set():
            self.draw()
            self._stop.wait(self.interval)

    def __enter__(self) -> "LiveStatus":
        global _active
        clearing = {id(self.stream): _Clearing(self, self.stream)}
        for owner, name in ((sys, "stderr"), (sys, "stdout")):
            stream = getattr(owner, name)
            if stream is not None and _same_terminal(stream, self.stream):
                wrapper = clearing.setdefault(id(stream), _Clearing(self, stream))
                self._saved.append((owner, name, stream))
                setattr(owner, name, wrapper)
        for handler in logging.getLogger().handlers:
            stream = getattr(handler, "stream", None)
            if isinstance(handler, logging.StreamHandler) and stream is not None:
                if _same_terminal(stream, self.stream):
                    wrapper = clearing.setdefault(id(stream), _Clearing(self, stream))
                    self._saved.append((handler, "stream", stream))
                    handler.stream = wrapper
        _active = self
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        global _active
        self._stop.set()
        self._thread.join()
        with self.lock:
            self.clear_locked()
            self.stream.flush()
            for owner, name, stream in reversed(self._saved):
                setattr(owner, name, stream)
            self._saved.clear()
            _active = None


def _same_terminal(stream: Any, terminal: IO[str]) -> bool:
    if stream is terminal:
        return True
    try:
        return bool(stream.isatty()) and os.path.samestat(
            os.fstat(stream.fileno()), os.fstat(terminal.fileno())
        )
    except (AttributeError, OSError, ValueError):
        return False


@contextlib.contextmanager
def step(name: str) -> Iterator[None]:
    """Show name as the current step while the block runs."""
    status = _active
    if status is None:
        yield
        return
    with status.lock:
        status.steps.append(name)
    try:
        yield
    finally:
        with status.lock:
            # The most recent entry: another thread may have pushed after it.
            del status.steps[len(status.steps) - 1 - status.steps[::-1].index(name)]
            status.detail = ""


def detail(text: str) -> None:
    """Show text (a progress count) after the elapsed time."""
    if _active is not None:
        _active.detail = text
