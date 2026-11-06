"""The HTTP/JSON front end: ``/v1`` on 127.0.0.1, with a bearer token.

Every request but ``GET /v1/health`` needs ``Authorization: Bearer <token>``;
requests naming another host (DNS rebinding) or sent from a web page of
another origin are refused, and so are POST bodies that aren't JSON.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import re
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from wgsextract_cli import __version__

from .jobs import Job, JobManager
from .operations import PathGuard, RequestError, find, operations, params_from_json
from .queries import QUERIES

MAX_BODY_BYTES = 1 << 20
MAX_WAIT_SECONDS = 600.0
SSE_KEEPALIVE_SECONDS = 15.0


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, port: int, token: str, manager: JobManager, guard: PathGuard
    ) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.manager = manager
        self.guard = guard

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    @property
    def hosts(self) -> frozenset[str]:
        port = self.server_address[1]
        return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def job_view(job: Job, query: dict[str, list[str]]) -> dict[str, Any]:
    return job.view(full=_flag(query, "full"))


def _flag(query: dict[str, list[str]], name: str) -> bool:
    return query.get(name, ["0"])[-1].lower() in ("1", "true", "yes")


def _wait(query: dict[str, list[str]]) -> float:
    try:
        seconds = float(query.get("wait", ["0"])[-1])
        if not math.isfinite(seconds):
            raise ValueError
    except ValueError:
        raise RequestError(400, "usage", "wait must be a number of seconds.") from None
    return min(max(seconds, 0.0), MAX_WAIT_SECONDS)


Route = Callable[["Handler", re.Match[str], dict[str, list[str]]], None]


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"
    server_version = f"wgsextract/{__version__}"

    # Plumbing ---------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        logging.debug("HTTP %s", format % args)

    def _send_json(self, status: int, body: object, **headers: str) -> None:
        data = json.dumps(body, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, error: RequestError) -> None:
        # The request's body may be unread: don't parse it as the next request.
        self.close_connection = True
        headers = {"Connection": "close"}
        if error.status == 401:
            headers["WWW_Authenticate"] = "Bearer"
        self._send_json(error.status, error.to_dict(), **headers)

    def _check_request(self, path: str) -> None:
        if self.headers.get("Host", "") not in self.server.hosts:
            raise RequestError(403, "bad_host", "Unexpected Host header.")
        origin = self.headers.get("Origin")
        if origin and origin.removeprefix("http://") not in self.server.hosts:
            raise RequestError(403, "bad_origin", "Cross-origin requests are refused.")
        if path == "/v1/health":
            return
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), self.server.token.encode()
        ):
            raise RequestError(
                401,
                "unauthorized",
                "Send 'Authorization: Bearer <token>'; `wgsextract serve` "
                "printed the token and saved it in its server.json.",
            )

    def _body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            raise RequestError(400, "bad_length", "Invalid Content-Length.")
        if length > MAX_BODY_BYTES:
            raise RequestError(413, "too_large", "The request body is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            raise RequestError(
                415, "not_json", "Send the parameters as application/json."
            )
        try:
            return json.loads(raw)
        except ValueError as e:
            raise RequestError(400, "bad_json", f"Invalid JSON: {e}") from None

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            try:
                self._check_request(url.path)
                for route_method, pattern, route in ROUTES:
                    match = pattern.fullmatch(url.path)
                    if match and route_method == method:
                        route(self, match, query)
                        return
                raise RequestError(404, "not_found", f"No route {method} {url.path}.")
            except RequestError as e:
                self._send_error(e)
            except ConnectionError:
                raise
            except Exception as e:
                logging.exception(f"{method} {url.path} failed")
                self._send_error(RequestError(500, "internal", f"Internal error: {e}"))
        except ConnectionError:
            # The client went away (closed, reset or aborted); nobody to answer.
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # Routes ----------------------------------------------------------------

    def health(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        self._send_json(200, {"ok": True, "version": __version__})

    def index(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        self._send_json(
            200,
            {
                "name": "wgsextract",
                "version": __version__,
                "ops": "/v1/ops",
                "jobs": "/v1/jobs",
                "openapi": "/v1/openapi.json",
                "lookups": [f"/v1/{q.route}" for q in QUERIES.values()],
                "allowed_paths": [str(p) for p in self.server.guard.roots],
            },
        )

    def openapi(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        self._send_json(200, openapi_document(self.server.url))

    def lookup(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        (lookup,) = (q for q in QUERIES.values() if q.route == match["route"])
        arguments = lookup.from_query_string(query)
        self._send_json(200, lookup.run(self.server.guard, arguments))

    def list_ops(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        self._send_json(200, {"ops": [op.describe() for op in operations().values()]})

    def describe_op(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        op = find(match["op"])
        self._send_json(
            200, {**op.describe(), "command": op.command, "params": op.schema()}
        )

    def run_op(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        op = find(match["op"])
        wait = _wait(query)
        body = self._body()
        params = params_from_json(op, body)
        self.server.guard.check(params)
        job = self.server.manager.submit(op, body if isinstance(body, dict) else {})
        if wait:
            job = self.server.manager.wait(job.id, wait) or job
        self._send_json(200 if job.done else 202, job_view(job, query))

    def _job(self, match: re.Match[str]) -> Job:
        job = self.server.manager.get(match["id"])
        if job is None:
            raise RequestError(404, "unknown_job", f"No job {match['id']!r}.")
        return job

    def list_jobs(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        jobs = self.server.manager.jobs()
        status = query.get("status", [None])[-1]
        self._send_json(
            200,
            {
                "jobs": [
                    {
                        k: v
                        for k, v in (
                            ("id", j.id),
                            ("op", j.op),
                            ("status", j.status),
                            ("created", j.created),
                            ("finished", j.finished),
                        )
                        if v is not None
                    }
                    for j in jobs
                    if status is None or j.status == status
                ]
            },
        )

    def get_job(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        job = self._job(match)
        wait = _wait(query)
        if wait:
            job = self.server.manager.wait(job.id, wait) or job
        self._send_json(200, job_view(job, query))

    def cancel_job(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        job = self._job(match)
        if job.done:
            raise RequestError(
                409, "finished", f"Job {job.id} already finished ({job.status})."
            )
        job = self.server.manager.cancel(job.id) or job
        self._send_json(200 if job.done else 202, job_view(job, query))

    def rerun_job(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        job = self._job(match)
        op = find(job.op)
        self.server.guard.check(params_from_json(op, job.params))
        new = self.server.manager.submit(op, job.params)
        self._send_json(202, job_view(new, query))

    def job_log(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        job = self._job(match)
        try:
            lines = (
                self.server.manager.log_path(job.id)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except FileNotFoundError:
            lines = []
        try:
            tail = int(query.get("tail", ["200"])[-1])
        except ValueError:
            raise RequestError(400, "usage", "tail must be an integer.") from None
        data = ("\n".join(lines[-tail:] if tail > 0 else lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def job_events(self, match: re.Match[str], query: dict[str, list[str]]) -> None:
        """Server-sent events: each event's ``id`` is its ``seq``; the stream
        ends after the job's final status. ``Last-Event-ID`` resumes."""
        job = self._job(match)
        try:
            after = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        while True:
            events, finished = self.server.manager.events(
                job.id, after, SSE_KEEPALIVE_SECONDS
            )
            for event in events:
                after = event["seq"]
                data = json.dumps(event, separators=(",", ":"))
                self.wfile.write(
                    f"id: {after}\nevent: {event['type']}\ndata: {data}\n\n".encode()
                )
            if not events:
                self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
            # The final status event is recorded together with the job finishing.
            if finished:
                return


_OP = r"(?P<op>[a-z0-9-]+(?:/[a-z0-9-]+)*)"
_ID = r"(?P<id>[A-Za-z0-9-]+)"
ROUTES: list[tuple[str, re.Pattern[str], Route]] = [
    (method, re.compile(pattern), route)
    for method, pattern, route in (
        ("GET", "/v1/health", Handler.health),
        ("GET", "/v1/?", Handler.index),
        ("GET", "/v1/openapi.json", Handler.openapi),
        ("GET", "/v1/ops", Handler.list_ops),
        ("GET", f"/v1/ops/{_OP}", Handler.describe_op),
        ("POST", f"/v1/ops/{_OP}", Handler.run_op),
        ("GET", "/v1/jobs", Handler.list_jobs),
        ("GET", f"/v1/jobs/{_ID}", Handler.get_job),
        ("DELETE", f"/v1/jobs/{_ID}", Handler.cancel_job),
        ("POST", f"/v1/jobs/{_ID}/rerun", Handler.rerun_job),
        ("GET", f"/v1/jobs/{_ID}/log", Handler.job_log),
        ("GET", f"/v1/jobs/{_ID}/events", Handler.job_events),
        ("GET", "/v1/(?P<route>manifest|variants|report)", Handler.lookup),
    )
]


# OpenAPI ---------------------------------------------------------------------

_ERROR = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
            "required": ["code", "message"],
        }
    },
    "required": ["error"],
}
_JOB = {
    "type": "object",
    "description": "A job. `result` is its result manifest (wgsextract.result/1, "
    "docs/result-manifest.md), without `provenance` unless ?full=1.",
    "properties": {
        "id": {"type": "string"},
        "op": {"type": "string"},
        "kind": {"enum": ["query", "job"]},
        "status": {
            "enum": [
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            ]
        },
        "created": {"type": "string", "format": "date-time"},
        "started": {"type": "string", "format": "date-time"},
        "finished": {"type": "string", "format": "date-time"},
        "cancel_requested": {"type": "string", "format": "date-time"},
        "exit_code": {"type": "integer"},
        "progress": {
            "type": "object",
            "properties": {
                "stage": {"type": "string"},
                "message": {"type": "string"},
                "fraction": {"type": "number"},
            },
        },
        "error": {
            "type": "object",
            "properties": {"kind": {"type": "string"}, "message": {"type": "string"}},
        },
        "params": {"type": "object", "description": "With ?full=1."},
        "result": {"type": "object"},
    },
    "required": ["id", "op", "kind", "status", "created"],
}


def _response(description: str, schema: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}
        },
    }


def _errors(*codes: int) -> dict[str, Any]:
    return {str(code): _response(HTTPStatus(code).phrase, "Error") for code in codes}


_WAIT = {
    "name": "wait",
    "in": "query",
    "description": "Wait up to this many seconds for the job to finish.",
    "schema": {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS},
}
_FULL = {
    "name": "full",
    "in": "query",
    "description": "Include the params and the result's provenance.",
    "schema": {"type": "boolean"},
}
_JOB_ID = {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}


_DELETES = (
    " It can delete data it didn't make, so the server refuses it (403 "
    "destructive) unless started with `serve --allow-destructive`."
)


def openapi_document(server_url: str = "http://127.0.0.1") -> dict[str, Any]:
    """The OpenAPI 3.1 description of the HTTP API, generated from the API's
    Params classes."""
    schemas: dict[str, Any] = {"Error": _ERROR, "Job": _JOB}
    paths: dict[str, Any] = {}
    for op in operations().values():
        name = op.params.__name__
        schemas[name] = op.schema()
        paths[f"/v1/ops/{op.name}"] = {
            "get": {
                "operationId": f"describe_{op.tool_name}",
                "summary": f"Describe {op.command}",
                "responses": {"200": {"description": "The operation and its params"}},
            },
            "post": {
                "operationId": op.tool_name,
                "summary": op.summary,
                "description": f"Runs `wgsextract {op.command}` as a {op.kind}."
                + (_DELETES if op.may_delete else ""),
                "parameters": [_WAIT, _FULL],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{name}"}
                        }
                    },
                },
                "responses": {
                    "200": _response("Finished within ?wait", "Job"),
                    "202": _response("Queued or running", "Job"),
                    **_errors(400, 401, 403, 404),
                },
            },
        }
    for lookup in QUERIES.values():
        paths[f"/v1/{lookup.route}"] = {
            "get": {
                "operationId": lookup.name,
                "summary": lookup.summary,
                "description": "Read-only; array parameters are comma-separated.",
                "parameters": [
                    {
                        "name": name,
                        "in": "query",
                        "required": name in lookup.required,
                        "schema": spec,
                        **({"explode": False} if spec["type"] == "array" else {}),
                    }
                    for name, spec in lookup.properties.items()
                ],
                "responses": {
                    "200": {"description": "The answer"},
                    **_errors(400, 401, 403, 404),
                },
            }
        }
    paths.update(
        {
            "/v1/jobs": {
                "get": {
                    "operationId": "list_jobs",
                    "summary": "List jobs (id, op, status, times)",
                    "parameters": [
                        {"name": "status", "in": "query", "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "The jobs"}},
                }
            },
            "/v1/jobs/{id}": {
                "parameters": [_JOB_ID],
                "get": {
                    "operationId": "get_job",
                    "summary": "A job, with its result once finished",
                    "parameters": [_WAIT, _FULL],
                    "responses": {
                        "200": _response("The job", "Job"),
                        **_errors(401, 404),
                    },
                },
                "delete": {
                    "operationId": "cancel_job",
                    "summary": "Cancel a queued or running job",
                    "responses": {
                        "200": _response("Cancelled", "Job"),
                        "202": _response("Cancelling", "Job"),
                        **_errors(401, 404, 409),
                    },
                },
            },
            "/v1/jobs/{id}/rerun": {
                "parameters": [_JOB_ID],
                "post": {
                    "operationId": "rerun_job",
                    "summary": "Run a job's params again; long workflows resume "
                    "from their completed steps",
                    "responses": {
                        "202": _response("The new job", "Job"),
                        **_errors(401, 403, 404),
                    },
                },
            },
            "/v1/jobs/{id}/events": {
                "parameters": [_JOB_ID],
                "get": {
                    "operationId": "job_events",
                    "summary": "Progress as server-sent events (status, progress); "
                    "ends after the final status",
                    "responses": {
                        "200": {
                            "description": "An event stream",
                            "content": {"text/event-stream": {}},
                        }
                    },
                },
            },
            "/v1/jobs/{id}/log": {
                "parameters": [
                    _JOB_ID,
                    {"name": "tail", "in": "query", "schema": {"type": "integer"}},
                ],
                "get": {
                    "operationId": "job_log",
                    "summary": "The last lines of the job's log",
                    "responses": {
                        "200": {
                            "description": "Log text",
                            "content": {"text/plain": {}},
                        }
                    },
                },
            },
        }
    )
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "WGS Extract",
            "version": __version__,
            "description": "The typed WGS Extract API (docs/api.md) over local "
            "HTTP: operations run as jobs. See docs/server.md.",
        },
        "servers": [{"url": server_url}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        },
    }
