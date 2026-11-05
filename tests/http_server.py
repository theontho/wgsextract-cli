"""A local HTTP server with byte ranges, for download tests that never reach
the network."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class RangeServer:
    """Serves ``files[path]``, or ``data`` for any other path (404 when None),
    with byte ranges, and records each request.

    drops: GETs left to cut off half-way. drop_starts: ranged GETs starting at
    these offsets are always cut off. head_status/get_status: answer HEAD/GET
    with this error.
    """

    def __init__(self, data: bytes | None = b"", files: dict[str, bytes] | None = None):
        self.data = data
        self.files = dict(files or {})
        self.drops = 0
        self.drop_starts: set[int] = set()
        self.ignore_ranges = False
        self.get_status: int | None = None
        self.head_status: int | None = None
        self.bare_416 = False  # leave Content-Range out of 416s, as Ensembl does
        self.requests: list[tuple[str, str | None]] = []
        self.paths: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self) -> bytes | None:
                path = urlsplit(self.path).path.lstrip("/")
                server.paths.append(path)
                return server.files.get(path, server.data)

            def do_HEAD(self):
                server.requests.append(("HEAD", None))
                data = self._body()
                if server.head_status or data is None:
                    self.send_error(server.head_status or 404)
                    return
                self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()

            def do_GET(self):
                spec = self.headers.get("Range")
                server.requests.append(("GET", spec))
                data = self._body()
                if server.get_status or data is None:
                    self.send_error(server.get_status or 404)
                    return
                start = 0
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
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = self.url_for("hs38.fa.gz")
        # A short poll keeps shutdown() from waiting out the default half second.
        threading.Thread(
            target=self.httpd.serve_forever, args=(0.01,), daemon=True
        ).start()

    def url_for(self, path: str) -> str:
        return f"{self.base}/{path}"

    def gets(self) -> list[str | None]:
        return [spec for method, spec in self.requests if method == "GET"]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@contextmanager
def serving(
    data: bytes | None = b"", files: dict[str, bytes] | None = None
) -> Iterator[RangeServer]:
    server = RangeServer(data, files)
    try:
        yield server
    finally:
        server.close()
