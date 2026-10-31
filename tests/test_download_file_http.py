"""download_file against a real local HTTP server: resume, restarts, cancellation,
checksums and the dev cache. Nothing here reaches the network."""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError

import pytest

from wgsextract_cli.core import dev_download_cache
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.ref_library import downloads, segmented
from wgsextract_cli.core.ref_library.segmented import ranged_size

# More than one copy_response_to_file chunk (256 KiB), so a cancel can land mid-file.
DATA = os.urandom(600_000)


class _Server:
    """Serves DATA with byte ranges and records each request.

    drops: GETs left to cut off half-way. drop_starts: ranged GETs starting at
    these offsets are always cut off. head_status: answer HEAD with this error.
    """

    def __init__(self, data: bytes = DATA):
        self.data = data
        self.drops = 0
        self.drop_starts: set[int] = set()
        self.ignore_ranges = False
        self.get_status: int | None = None
        self.head_status: int | None = None
        self.bare_416 = False  # leave Content-Range out of 416s, as Ensembl does
        self.requests: list[tuple[str, str | None]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                server.requests.append(("HEAD", None))
                if server.head_status:
                    self.send_error(server.head_status)
                    return
                self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(server.data)))
                self.end_headers()

            def do_GET(self):
                spec = self.headers.get("Range")
                server.requests.append(("GET", spec))
                if server.get_status:
                    self.send_error(server.get_status)
                    return
                data, start = server.data, 0
                body, code = data, 200
                if spec and not server.ignore_ranges:
                    first, _, last = spec.removeprefix("bytes=").partition("-")
                    start = int(first)
                    if start >= len(data):
                        self.send_response(416)
                        if not server.bare_416:
                            self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    end = int(last) + 1 if last else len(data)
                    body, code = data[start:end], 206
                cut = server.drops > 0 or (code == 206 and start in server.drop_starts)
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if cut:
                    server.drops = max(0, server.drops - 1)
                    self.wfile.write(body[: len(body) // 2])
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/hs38.fa.gz"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def gets(self) -> list[str | None]:
        return [spec for method, spec in self.requests if method == "GET"]


@pytest.fixture
def server(monkeypatch):
    """One-connection urllib downloads (no curl, no GitHub lookup, no sleeping)."""
    srv = _Server()
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda u: None
    )
    monkeypatch.setattr(downloads, "curl_progress_args", lambda: ["--silent"])
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


@pytest.mark.parametrize("entrypoint", ["benchmark", "examples"])
def test_user_download_entrypoints_resume_without_exposing_partial_files(
    server, tmp_path, entrypoint
):
    from wgsextract_cli.commands import examples
    from wgsextract_cli.commands.benchmark import datasets

    dest = tmp_path / "example.bam"
    server.drops = 1
    if entrypoint == "benchmark":
        datasets._download_file(server.url, dest)
    else:
        examples._download_file(server.url, dest, "https")
    assert dest.read_bytes() == DATA
    assert server.gets() == [None, f"bytes={len(DATA) // 2}-"]
    assert not Path(str(dest) + ".partial").exists()


def test_explicit_checksum_verifies_before_replacing_destination(
    server, tmp_path, monkeypatch
):
    dest = tmp_path / "dataset.zip"
    dest.write_bytes(b"previous verified download")
    monkeypatch.setattr(
        downloads,
        "resolve_github_release_asset_sha256",
        lambda _: pytest.fail("Explicit checksum must not need GitHub metadata"),
    )
    assert not downloads.download_file(server.url, str(dest), expected_sha256="0" * 64)
    assert dest.read_bytes() == b"previous verified download"
    assert not Path(str(dest) + ".partial").exists()
    assert downloads.download_file(
        server.url, str(dest), expected_sha256=hashlib.sha256(DATA).hexdigest().upper()
    )
    assert dest.read_bytes() == DATA


@pytest.mark.parametrize("checksum", ["a" * 63, "x" * 64])
def test_invalid_explicit_checksum_fails_before_network_or_destination_changes(
    server, tmp_path, checksum
):
    from wgsextract_cli.core.utils import WGSExtractError

    dest = tmp_path / "dataset.zip"
    dest.write_bytes(b"existing data")
    with pytest.raises(WGSExtractError, match="64 hex digits"):
        downloads.download_file(server.url, str(dest), expected_sha256=checksum)
    assert dest.read_bytes() == b"existing data"
    assert server.gets() == []
    assert not Path(str(dest) + ".partial").exists()


def test_benchmark_md5_failure_drops_unchecked_shared_cache(
    server, tmp_path, monkeypatch
):
    from wgsextract_cli.commands.benchmark import datasets
    from wgsextract_cli.commands.benchmark.models import BenchmarkRemoteFile
    from wgsextract_cli.core.utils import WGSExtractError

    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setattr(
        dev_download_cache, "download_cache_root", lambda: tmp_path / "dev-cache"
    )
    good = b"verified benchmark data"
    remote = BenchmarkRemoteFile(
        "bam", server.url, "sample.bam", hashlib.md5(good).hexdigest()
    )
    destination = tmp_path / remote.filename
    unchecked = dev_download_cache.cached_download_path(server.url, destination)
    server.data = b"corrupt benchmark data"
    with pytest.raises(WGSExtractError, match="MD5 mismatch"):
        datasets._cached_remote_dataset_file(remote, tmp_path)
    assert not destination.exists()
    assert not unchecked.exists()

    server.data = good
    assert datasets._cached_remote_dataset_file(remote, tmp_path).read_bytes() == good
    assert server.gets() == [None, None]


@pytest.fixture(params=("urllib", "curl", "segmented"))
def transport(request, server, monkeypatch):
    if request.param == "curl":
        if shutil.which("curl") is None:
            pytest.skip("needs curl")
        monkeypatch.setattr(downloads, "curl_progress_args", lambda: ["--progress-bar"])
    elif request.param == "segmented":
        monkeypatch.setattr(segmented, "ranged_size", ranged_size)
        monkeypatch.setattr(segmented, "SEGMENTED_MIN_SIZE", 1)
        monkeypatch.setattr(segmented, "SEGMENT_SIZE", 100_000)
    return server, request.param


@pytest.mark.parametrize(
    ("same_size", "checksum"),
    [(False, False), (True, False), (False, True)],
)
def test_redownload_replaces_existing_dest_only_after_verification(
    transport, tmp_path, monkeypatch, same_size, checksum
):
    server, kind = transport
    dest = tmp_path / "hs38.fa.gz"
    partial = Path(str(dest) + ".partial")
    old = b"x" * len(DATA) if same_size else b"stale bytes from an earlier release"
    dest.write_bytes(old)
    digest = hashlib.sha256(DATA).hexdigest() if checksum else None
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda _: digest
    )
    real_verify = downloads.verify_download_sha256

    def verify(path, expected):
        assert dest.read_bytes() == old
        assert Path(path) == partial
        return real_verify(path, expected)

    monkeypatch.setattr(downloads, "verify_download_sha256", verify)
    assert downloads.download_file(server.url, str(dest))
    assert dest.read_bytes() == DATA
    assert not partial.exists()
    assert not Path(str(partial) + ".segments").exists()
    if kind != "segmented":
        assert server.gets() == [None]


def test_failed_checksum_preserves_existing_dest(transport, tmp_path, monkeypatch):
    server, _ = transport
    dest = tmp_path / "hs38.fa.gz"
    old = b"keep the previous complete reference"
    dest.write_bytes(old)
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda _: "0" * 64
    )
    assert not downloads.download_file(server.url, str(dest))
    assert dest.read_bytes() == old
    assert not Path(str(dest) + ".partial").exists()
    assert not Path(str(dest) + ".partial.segments").exists()


def test_download_streams_the_file_and_reports_progress(server, tmp_path):
    dest = tmp_path / "hs38.fa.gz"
    progress = []

    assert downloads.download_file(
        server.url, str(dest), progress_callback=lambda d, t, s: progress.append((d, t))
    )

    assert dest.read_bytes() == DATA
    assert not (tmp_path / "hs38.fa.gz.partial").exists()
    assert progress[-1] == (len(DATA), len(DATA))
    assert server.gets() == [None]


def test_a_dropped_connection_resumes_where_it_stopped(server, tmp_path):
    server.drops = 1
    dest = tmp_path / "hs38.fa.gz"

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert server.gets() == [None, f"bytes={len(DATA) // 2}-"]


def test_a_cancelled_download_keeps_its_partial_for_the_next_run(server, tmp_path):
    dest = tmp_path / "hs38.fa.gz"
    partial = tmp_path / "hs38.fa.gz.partial"
    cancel = threading.Event()

    assert not downloads.download_file(
        server.url, str(dest), lambda d, t, s: cancel.set(), cancel
    )

    assert not dest.exists()
    kept = partial.read_bytes()
    assert 0 < len(kept) < len(DATA) and DATA.startswith(kept)

    assert downloads.download_file(server.url, str(dest), lambda d, t, s: None)
    assert dest.read_bytes() == DATA
    assert server.gets()[-1] == f"bytes={len(kept)}-"


def test_a_server_that_ignores_ranges_restarts_the_file(server, tmp_path):
    server.ignore_ranges = True
    dest = tmp_path / "hs38.fa.gz"
    (tmp_path / "hs38.fa.gz.partial").write_bytes(b"stale bytes from elsewhere")

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert server.gets() == ["bytes=26-"]


def test_an_existing_complete_file_is_fetched_from_byte_zero(server, tmp_path):
    dest = tmp_path / "hs38.fa.gz"
    dest.write_bytes(DATA)

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert server.gets() == [None]


def test_a_bare_416_is_checked_against_the_size_from_head(server, tmp_path):
    server.bare_416 = True
    dest = tmp_path / "hs38.fa.gz"
    partial = Path(str(dest) + ".partial")
    partial.write_bytes(DATA)

    assert downloads.download_file(server.url, str(dest))
    assert server.requests == [("GET", f"bytes={len(DATA)}-"), ("HEAD", None)]

    # Without a size to compare, the file can't be trusted: fetch it again.
    server.head_status = 405
    server.requests.clear()
    partial.write_bytes(DATA)
    assert downloads.download_file(server.url, str(dest))
    assert dest.read_bytes() == DATA
    assert server.gets() == [f"bytes={len(DATA)}-", None]


def test_an_empty_partial_starts_from_the_first_byte(server, tmp_path):
    (tmp_path / "hs38.fa.gz.partial").write_bytes(b"")

    assert downloads.download_file(server.url, str(tmp_path / "hs38.fa.gz"))

    assert (tmp_path / "hs38.fa.gz").read_bytes() == DATA
    assert server.gets() == [None]


def test_a_server_error_is_retried(server, tmp_path, monkeypatch):
    monkeypatch.setattr(downloads, "DOWNLOAD_ATTEMPTS", 3)
    server.get_status = 503

    assert not downloads.download_file(server.url, str(tmp_path / "hs38.fa.gz"))
    assert server.gets() == [None, None, None]


def test_an_http_error_fails_at_once_without_a_file(server, tmp_path):
    server.get_status = 404
    dest = tmp_path / "hs38.fa.gz"

    assert not downloads.download_file(server.url, str(dest))

    assert not dest.exists()
    assert server.gets() == [None]


def test_a_transfer_that_keeps_dropping_gives_up_and_keeps_its_bytes(
    server, tmp_path, monkeypatch
):
    monkeypatch.setattr(downloads, "DOWNLOAD_ATTEMPTS", 3)
    server.drops = 99
    dest = tmp_path / "hs38.fa.gz"

    assert not downloads.download_file(server.url, str(dest))

    kept = (tmp_path / "hs38.fa.gz.partial").read_bytes()
    assert len(server.gets()) == 3
    # Each attempt keeps half of what was left.
    assert len(kept) == len(DATA) - len(DATA) // 8 and DATA.startswith(kept)


def test_a_reconnect_during_a_network_blip_is_retried(server, tmp_path, monkeypatch):
    server.drops = 1
    real_urlopen = downloads.urlopen
    calls = []

    def flaky_urlopen(request, timeout):
        calls.append(request.headers.get("Range"))
        if len(calls) == 2:
            raise URLError(OSError(101, "Network is unreachable"))
        return real_urlopen(request, timeout=timeout)

    monkeypatch.setattr(downloads, "urlopen", flaky_urlopen)
    dest = tmp_path / "hs38.fa.gz"

    assert downloads.download_file(server.url, str(dest))
    assert dest.read_bytes() == DATA


def test_a_checksum_mismatch_deletes_the_download(server, tmp_path, monkeypatch):
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda u: "0" * 64
    )
    dest = tmp_path / "hs38.fa.gz"

    assert not downloads.download_file(server.url, str(dest))

    assert not dest.exists()
    assert not (tmp_path / "hs38.fa.gz.partial").exists()


def test_curl_failure_falls_back_to_urllib_and_resumes_its_bytes(
    server, tmp_path, monkeypatch
):
    """With a terminal, curl downloads to dest.partial; when it fails part-way,
    the urllib fallback resumes from what curl wrote."""
    monkeypatch.setattr(downloads, "curl_progress_args", lambda: ["--progress-bar"])
    dest = tmp_path / "hs38.fa.gz"
    commands = []

    def half_curl(cmd, capture_output=False):
        commands.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(DATA[:1000])
        raise subprocess.CalledProcessError(18, cmd)

    monkeypatch.setattr(downloads, "run_command", half_curl)

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert "-C" not in commands[0]
    assert server.gets() == ["bytes=1000-"]


@pytest.mark.parametrize("existing", [False, True])
def test_an_interrupted_curl_preserves_the_final_name(
    server, tmp_path, monkeypatch, existing
):
    monkeypatch.setattr(downloads, "curl_progress_args", lambda: ["--progress-bar"])
    dest = tmp_path / "hs38.fa.gz"
    if existing:
        dest.write_bytes(b"previous complete file")

    def interrupted_curl(cmd, capture_output=False):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(DATA[:1000])
        raise KeyboardInterrupt

    monkeypatch.setattr(downloads, "run_command", interrupted_curl)

    with pytest.raises(KeyboardInterrupt):
        downloads.download_file(server.url, str(dest))
    if existing:
        assert dest.read_bytes() == b"previous complete file"
    else:
        assert not dest.exists()
    assert (tmp_path / "hs38.fa.gz.partial").read_bytes() == DATA[:1000]


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
@pytest.mark.parametrize("have", [0, 1000, len(DATA)])
def test_curl_resumes_into_the_partial_and_finishes_at_dest(
    server, tmp_path, monkeypatch, have
):
    monkeypatch.setattr(downloads, "curl_progress_args", lambda: ["--progress-bar"])
    dest = tmp_path / "hs38.fa.gz"
    if have:
        (tmp_path / "hs38.fa.gz.partial").write_bytes(DATA[:have])

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert not (tmp_path / "hs38.fa.gz.partial").exists()
    assert server.gets()[0] == (f"bytes={have}-" if have else None)


def test_a_poisoned_dev_cache_item_is_dropped_and_fetched_again(
    server, tmp_path, monkeypatch
):
    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    digest = hashlib.sha256(DATA).hexdigest()
    monkeypatch.setattr(
        downloads, "resolve_github_release_asset_sha256", lambda u: digest
    )
    bad = tmp_path / "bad.fa.gz"
    bad.write_bytes(b"not the reference")
    hint = f"sha256:{digest}"
    cached = dev_download_cache.store_download_in_dev_cache(
        server.url, bad, checksum_hint=hint
    )
    assert cached is not None
    dest = tmp_path / "out" / "hs38.fa.gz"

    assert downloads.download_file(server.url, str(dest))

    assert dest.read_bytes() == DATA
    assert server.gets() == [None]
    # The good download replaced the bad cache item.
    assert cached.read_bytes() == DATA


@pytest.fixture
def segmented_server(server, monkeypatch):
    """Ranged downloads in 1000-byte pieces, one attempt each."""
    server.data = DATA[:10_000]
    monkeypatch.setattr(segmented, "ranged_size", ranged_size)
    monkeypatch.setattr(segmented, "SEGMENTED_MIN_SIZE", 1)
    monkeypatch.setattr(segmented, "SEGMENT_SIZE", 1000)
    monkeypatch.setattr(segmented, "ATTEMPTS", 1)
    monkeypatch.setattr(segmented.time, "sleep", lambda s: None)
    return server


def test_a_cancelled_segmented_download_keeps_its_pieces(segmented_server, tmp_path):
    srv = segmented_server
    dest = tmp_path / "hs38.fa.gz"
    cancel = threading.Event()
    cancel.set()

    assert not downloads.download_file(srv.url, str(dest), cancel_event=cancel)

    assert not dest.exists()
    state = json.loads((tmp_path / "hs38.fa.gz.partial.segments").read_text())
    assert state["url"] == srv.url and state["size"] == len(srv.data)

    srv.requests.clear()
    assert downloads.download_file(srv.url, str(dest))
    assert dest.read_bytes() == srv.data
    fetched = [r for r in srv.gets() if r]
    assert len(fetched) == 10 - sum(
        1 for count in state["received"].values() if count == 1000
    )


def test_an_unfinished_segmented_partial_is_never_taken_as_complete(
    segmented_server, tmp_path
):
    srv = segmented_server
    srv.drop_starts = {5000}
    dest = tmp_path / "hs38.fa.gz"
    assert not downloads.download_file(srv.url, str(dest))
    assert (tmp_path / "hs38.fa.gz.partial.segments").exists()

    srv.drop_starts = set()
    srv.head_status = 405  # the size probe fails this time

    downloads.download_file(srv.url, str(dest))
    assert not dest.exists() or dest.read_bytes() == srv.data
