"""A job's worker process: runs one operation through the typed API.

Reads ``{"op", "params", "config", "debug", "allowed"}`` as one JSON line on
stdin, then
writes ``{"event": "progress", ...}`` lines and a final ``{"event":
"result", "result": <manifest>}`` to stdout. A ``cancel`` line on stdin, or
stdin closing (the server went away), cancels the operation.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import nullcontext
from typing import Any, TextIO

from wgsextract_cli import api


def _watch(stdin: TextIO, token: api.CancelToken) -> None:
    for line in stdin:
        if line.strip() == "cancel":
            break
    token.cancel()


def _private_pipes() -> tuple[TextIO, TextIO]:
    """Move the server's pipes off stdin and stdout, so the tools a job starts
    don't inherit them: stray tool output would corrupt the protocol, and on
    Windows a child that touches a pipe this process is reading blocks. Tools
    get the null device as stdin and the job log as stdout."""
    control_in = os.fdopen(os.dup(0), encoding="utf-8")
    control_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    return control_in, control_out


def main() -> int:
    from wgsextract_cli.cli.bootstrap import configure_logging
    from wgsextract_cli.core import read_scope
    from wgsextract_cli.core.config import settings
    from wgsextract_cli.core.keep_awake import keep_awake

    from .operations import PathGuard, find, params_from_json

    control_in, out = _private_pipes()
    request = json.loads(control_in.readline())
    configure_logging(debug=bool(request.get("debug")))
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
    threading.Thread(target=_watch, args=(control_in, token), daemon=True).start()
    api.use_config(request.get("config"))
    op = find(request["op"])
    params = params_from_json(op, request["params"])
    allowed = request.get("allowed")
    scope = (
        nullcontext()
        if allowed is None
        else read_scope.limited_to(PathGuard(allowed).allows)
    )
    with scope, keep_awake(bool(settings.get("keep_awake"))):
        result = op.run(params, progress=progress, cancel=token, check=False)
    emit({"event": "result", "result": result.to_dict()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
