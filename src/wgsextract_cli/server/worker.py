"""A job's worker process: runs one operation through the typed API.

Reads ``{"op", "params", "config", "debug"}`` as one JSON line on stdin, then
writes ``{"event": "progress", ...}`` lines and a final ``{"event":
"result", "result": <manifest>}`` to stdout. A ``cancel`` line on stdin, or
stdin closing (the server went away), cancels the operation.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, TextIO

from wgsextract_cli import api


def _watch(stdin: TextIO, token: api.CancelToken) -> None:
    for line in stdin:
        if line.strip() == "cancel":
            break
    token.cancel()


def main() -> int:
    from wgsextract_cli.cli.bootstrap import configure_logging

    from .operations import find, params_from_json

    request = json.loads(sys.stdin.readline())
    configure_logging(debug=bool(request.get("debug")))
    out = sys.stdout
    lock = threading.Lock()

    def emit(message: dict[str, Any]) -> None:
        with lock:
            out.write(json.dumps(message, default=str) + "\n")
            out.flush()

    def progress(event: api.ProgressEvent) -> None:
        emit(
            {
                "event": "progress",
                "stage": event.stage,
                "message": event.message,
                "fraction": event.fraction,
            }
        )

    token = api.CancelToken()
    threading.Thread(target=_watch, args=(sys.stdin, token), daemon=True).start()
    api.use_config(request.get("config"))
    op = find(request["op"])
    params = params_from_json(op, request["params"])
    result = op.run(params, progress=progress, cancel=token, check=False)
    emit({"event": "result", "result": result.to_dict()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
