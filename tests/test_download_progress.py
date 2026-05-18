from __future__ import annotations

import io
import logging

import pytest

from wgsextract_cli.core.download_progress import (
    DownloadCancelled,
    PercentProgressLogger,
    copy_response_to_file,
    curl_progress_args,
    require_http_url,
)


class FakeDownloadResponse:
    def __init__(self, payload: bytes, content_length: int | None) -> None:
        self._payload = io.BytesIO(payload)
        self._headers = {}
        if content_length is not None:
            self._headers["Content-Length"] = str(content_length)

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)

    def info(self) -> dict[str, str]:
        return self._headers


class CancelAfterFirstCheck:
    def __init__(self) -> None:
        self._checks = 0

    def is_set(self) -> bool:
        self._checks += 1
        return self._checks > 1


def test_copy_response_to_file_logs_newline_progress_every_ten_percent(caplog):
    response = FakeDownloadResponse(b"x" * 100, 100)
    output = io.BytesIO()

    caplog.set_level(logging.INFO)
    copy_response_to_file(
        response,
        output,
        progress_label="reference.fa.gz",
        chunk_size=10,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert output.getvalue() == b"x" * 100
    assert any("reference.fa.gz download progress: 10%" in msg for msg in messages)
    assert any("reference.fa.gz download progress: 100%" in msg for msg in messages)


def test_copy_response_to_file_logs_unknown_size_progress(caplog):
    response = FakeDownloadResponse(b"x" * 100, None)
    output = io.BytesIO()

    caplog.set_level(logging.INFO)
    copy_response_to_file(
        response,
        output,
        progress_label="dataset.zip",
        chunk_size=50,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert output.getvalue() == b"x" * 100
    assert any(
        "dataset.zip download progress: 50 B downloaded" in msg for msg in messages
    )
    assert any(
        "dataset.zip download complete: 100 B downloaded" in msg for msg in messages
    )


def test_progress_logger_rejects_invalid_step_percent():
    with pytest.raises(ValueError, match="step_percent"):
        PercentProgressLogger(step_percent=0)


def test_copy_response_to_file_rejects_invalid_chunk_size():
    response = FakeDownloadResponse(b"x" * 100, 100)
    output = io.BytesIO()

    with pytest.raises(ValueError, match="chunk_size"):
        copy_response_to_file(response, output, chunk_size=0)


def test_copy_response_to_file_raises_when_cancelled_after_partial_write():
    response = FakeDownloadResponse(b"x" * 100, 100)
    output = io.BytesIO()

    with pytest.raises(DownloadCancelled):
        copy_response_to_file(
            response,
            output,
            cancel_event=CancelAfterFirstCheck(),
            chunk_size=10,
        )

    assert output.getvalue() == b"x" * 10


def test_require_http_url_rejects_non_network_schemes():
    with pytest.raises(ValueError, match="Unsupported download URL scheme"):
        require_http_url("file:///tmp/reference.fa.gz")


def test_curl_progress_args_uses_progress_bar_only_for_tty(monkeypatch):
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert curl_progress_args() == ["--progress-bar"]

    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert curl_progress_args() == ["--silent", "--show-error"]
