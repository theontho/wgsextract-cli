from __future__ import annotations

import logging
import sys
import time
import urllib.parse
from collections.abc import Callable
from email.message import Message
from typing import BinaryIO, Protocol

ProgressCallback = Callable[[int, int, float], None]


class DownloadCancelled(Exception):
    """Raised when a cancellable download is interrupted."""


class DownloadResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def info(self) -> Message: ...


class CancelEvent(Protocol):
    def is_set(self) -> bool: ...


def format_bytes(value: int | float) -> str:
    """Return a compact byte count for user-facing progress output."""
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def require_http_url(url: str, label: str = "download URL") -> None:
    """Reject URL schemes that should not be opened by network download paths."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported {label} scheme '{parsed.scheme}' for {url!r}")


def curl_progress_args() -> list[str]:
    """Return curl progress flags appropriate for the current stderr."""
    if sys.stderr.isatty():
        return ["--progress-bar"]
    return ["--silent", "--show-error"]


class PercentProgressLogger:
    """Log download progress at newline-friendly percentage intervals."""

    def __init__(
        self,
        label: str = "Download",
        *,
        step_percent: int = 10,
        min_unknown_interval: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if step_percent <= 0:
            raise ValueError("step_percent must be greater than zero")
        self.label = label
        self.step_percent = step_percent
        self.min_unknown_interval = min_unknown_interval
        self.logger = logger or logging.getLogger(__name__)
        self._next_percent = step_percent
        self._last_unknown_report = 0.0

    def __call__(self, downloaded: int, total_size: int, speed: float) -> None:
        if total_size > 0:
            percent = min(100, int(downloaded * 100 / total_size))
            if percent < self._next_percent:
                return

            report_percent = min(
                100, (percent // self.step_percent) * self.step_percent
            )
            if report_percent < self._next_percent:
                report_percent = self._next_percent
            self.logger.info(
                "%s download progress: %d%% (%s / %s, %s/s)",
                self.label,
                report_percent,
                format_bytes(downloaded),
                format_bytes(total_size),
                format_bytes(speed),
            )
            self._next_percent = report_percent + self.step_percent
            return

        now = time.monotonic()
        if (
            self._last_unknown_report
            and now - self._last_unknown_report < self.min_unknown_interval
        ):
            return
        self._last_unknown_report = now
        self.logger.info(
            "%s download progress: %s downloaded (%s/s)",
            self.label,
            format_bytes(downloaded),
            format_bytes(speed),
        )

    def report_complete(self, downloaded: int, total_size: int, speed: float) -> None:
        if total_size > 0:
            return
        self.logger.info(
            "%s download complete: %s downloaded (%s/s)",
            self.label,
            format_bytes(downloaded),
            format_bytes(speed),
        )


def copy_response_to_file(
    response: DownloadResponse,
    output: BinaryIO,
    *,
    initial_size: int = 0,
    progress_callback: ProgressCallback | None = None,
    progress_label: str = "Download",
    cancel_event: CancelEvent | None = None,
    chunk_size: int = 1024 * 256,
) -> None:
    """Stream a URL response to a file while emitting progress updates."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    content_length = _response_content_length(response)
    total_size = initial_size + content_length if content_length > 0 else 0
    bytes_downloaded = initial_size
    start_time = time.monotonic()
    default_progress = (
        PercentProgressLogger(progress_label) if progress_callback is None else None
    )

    while True:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Download cancelled by user.")

        chunk = response.read(chunk_size)
        if not chunk:
            break

        output.write(chunk)
        bytes_downloaded += len(chunk)
        elapsed = time.monotonic() - start_time
        speed = (bytes_downloaded - initial_size) / elapsed if elapsed > 0 else 0.0

        if progress_callback is not None:
            progress_callback(bytes_downloaded, total_size, speed)
        if default_progress is not None:
            default_progress(bytes_downloaded, total_size, speed)

    if default_progress is not None:
        elapsed = time.monotonic() - start_time
        speed = (bytes_downloaded - initial_size) / elapsed if elapsed > 0 else 0.0
        default_progress.report_complete(bytes_downloaded, total_size, speed)


def _response_content_length(response: DownloadResponse) -> int:
    headers = response.info()
    if headers is None:
        return 0
    try:
        value = headers.get("Content-Length", 0)
    except AttributeError:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
