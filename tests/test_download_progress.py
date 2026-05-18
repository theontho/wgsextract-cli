from __future__ import annotations

import io
import logging

from wgsextract_cli.core.download_progress import copy_response_to_file


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
