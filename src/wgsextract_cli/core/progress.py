"""Stderr-only progress; never intercept a tool's data stream."""

import logging
import re
import threading
import time
from collections.abc import Callable

_READS = re.compile(r"(?:Processed|mapped)\s+(\d+)\s+(?:reads|sequences)", re.I)


class Heartbeat:
    def __init__(
        self,
        label: str,
        total: int | None = None,
        unit: str = "reads",
        *,
        interval: float = 60,
        scan: Callable[[], None] | None = None,
        emit: bool = True,
    ) -> None:
        self.label, self.total, self.unit = label, total, unit
        self.interval, self.scan = interval, scan
        self.emit = emit
        self.completed = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "Heartbeat":
        self.report()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self._scan()
        self.report()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._scan()
            self.report()

    def _scan(self) -> None:
        if self.scan:
            try:
                self.scan()
            except (OSError, ValueError) as error:
                logging.warning("Progress scan unavailable: %s", error)
                self.scan = None

    def advance(self, count: int = 1) -> None:
        with self._lock:
            self.completed += count

    def read_line(self, line: str) -> None:
        if match := _READS.search(line):
            self.advance(int(match.group(1)))

    def report(self) -> None:
        if not self.emit:
            return
        with self._lock:
            done = self.completed
        elapsed = max(0.0, time.monotonic() - self.started)
        progress = (
            f"{done:,} {self.unit}"
            if done or self.total
            else "running; processed-read count not yet available"
        )
        if self.total:
            progress = f"{done:,}/{self.total:,} {self.unit} ({min(100, done / self.total * 100):.1f}%)"
        eta = ""
        if self.total and 0 < done < self.total:
            eta = f", estimated {elapsed * (self.total - done) / done:.0f}s remaining"
        if self.total and done >= self.total and self.unit == "reads":
            eta = ", aligner input processed; downstream stages may still be running"
        logging.info(
            "%s heartbeat: %s, elapsed %.0fs%s", self.label, progress, elapsed, eta
        )
