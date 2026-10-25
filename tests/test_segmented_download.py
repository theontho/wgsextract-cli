import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wgsextract_cli.core.ref_library import downloads, segmented
from wgsextract_cli.core.ref_library.segmented import ranged_size

DATA = os.urandom(10_000)


class _Server:
    """Serves DATA with byte ranges; can cut responses short or ignore ranges."""

    def __init__(self):
        self.drop = {}  # piece start -> times left to cut a response in it short
        self.busy = {}  # piece start -> times left to answer it with a status
        self.ignore_ranges = False
        self.served = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(DATA)))
                self.end_headers()

            def do_GET(self):
                spec = self.headers.get("Range")
                if not spec or server.ignore_ranges:
                    body, code = DATA, 200
                else:
                    first, _, last = spec.removeprefix("bytes=").partition("-")
                    start = int(first)
                    end = int(last) + 1 if last else len(DATA)
                    body, code = DATA[start:end], 206
                    piece = start - start % 1000
                    if server.busy.get(piece):
                        server.busy[piece] -= 1
                        self.send_error(503 if piece else 404)
                        return
                    if server.drop.get(piece):
                        server.drop[piece] -= 1
                        self.send_response(206)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body[: len(body) // 2])
                        server.served += len(body) // 2
                        self.close_connection = True
                        return
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                server.served += len(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/big.bin"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server(monkeypatch):
    srv = _Server()
    monkeypatch.setattr(segmented, "ranged_size", ranged_size)
    monkeypatch.setattr(segmented, "SEGMENTED_MIN_SIZE", 1)
    monkeypatch.setattr(segmented, "SEGMENT_SIZE", 1000)
    monkeypatch.setattr(segmented, "_backoff", lambda attempt: 0)
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda u: None
    )
    monkeypatch.setattr(downloads, "restore_cached_download", lambda *a, **k: False)
    monkeypatch.setattr(downloads, "store_download_in_dev_cache", lambda *a, **k: None)
    yield srv
    srv.httpd.shutdown()


def test_large_file_comes_in_pieces(server, tmp_path):
    dest = tmp_path / "big.bin"

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert server.served == len(DATA)
    assert not (tmp_path / "big.bin.partial.segments").exists()


def test_a_cut_off_piece_is_resumed(server, tmp_path):
    server.drop = {0: 1, 4000: 2, 9000: 1}
    dest = tmp_path / "big.bin"

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA


def test_a_piece_the_server_is_too_busy_for_is_retried(server, tmp_path):
    server.busy = {3000: 2}
    dest = tmp_path / "big.bin"

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA


def test_a_piece_the_server_refuses_is_not_retried(server, tmp_path):
    server.busy = {0: 1}
    dest = tmp_path / "big.bin"

    assert not downloads.download_file(server.url, str(dest))
    assert server.busy == {0: 0}


def test_a_failed_download_resumes_on_the_next_run(server, tmp_path, monkeypatch):
    monkeypatch.setattr(segmented, "ATTEMPTS", 2)
    server.drop = {5000: 99}
    dest = tmp_path / "big.bin"

    assert not downloads.download_file(server.url, str(dest))
    assert not dest.exists()
    assert (tmp_path / "big.bin.partial.segments").exists()

    # What the state counts as received is on disk, byte for byte.
    state = json.loads((tmp_path / "big.bin.partial.segments").read_text())
    on_disk = (tmp_path / "big.bin.partial").read_bytes()
    for start, count in state["received"].items():
        piece = slice(int(start), int(start) + count)
        assert on_disk[piece] == DATA[piece]
    assert state["received"]["5000"] < 1000

    server.drop = {}
    server.served = 0
    assert downloads.download_file(server.url, str(dest))
    assert dest.read_bytes() == DATA
    # Only what hadn't arrived is fetched again.
    assert server.served == len(DATA) - sum(state["received"].values())


def test_a_server_that_ignores_ranges_gets_one_connection(server, tmp_path):
    server.ignore_ranges = True
    dest = tmp_path / "big.bin"

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert not (tmp_path / "big.bin.partial.segments").exists()
