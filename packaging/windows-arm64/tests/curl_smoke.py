"""Exercise the real curl HTTP redirect and gzip decompression paths."""

import argparse
import gzip
import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--curl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    payload = (
        "ARM236 reference transfer \N{GREEK SMALL LETTER ALPHA}\n" * 256
    ).encode()
    compressed = gzip.compress(payload, mtime=0)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/reference")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)

        def log_message(self, format: str, *values: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    output = args.output / "download.txt"
    try:
        subprocess.run(
            [
                str(args.curl.resolve()),
                "-q",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--compressed",
                "--output",
                str(output),
                f"http://127.0.0.1:{server.server_port}/redirect",
            ],
            check=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
    if output.read_bytes() != payload:
        raise RuntimeError("curl redirect/gzip transfer changed reference bytes")
    (args.output / "result.json").write_text(
        json.dumps(
            {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
