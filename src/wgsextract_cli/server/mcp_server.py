"""The MCP front end (``wgsextract serve --mcp``): JSON-RPC 2.0 over stdio, one
message per line, as the Model Context Protocol specifies.

Tools are the API's operations (``info``, ``vcf_snp``, ...) with their Params'
JSON Schema, the lookups (``read_manifest``, ``query_variants``,
``report_section``), ``list_jobs`` and ``job_status``. Only
read-only queries are offered unless ``--allow-jobs``; then every operation
is, and ``cancel_job``. A query tool waits for its result; a job tool returns
the started job, to follow with ``job_status``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any, TextIO

from wgsextract_cli import __version__

from .http_api import MAX_WAIT_SECONDS
from .jobs import Job, JobManager
from .operations import (
    Operation,
    PathGuard,
    RequestError,
    find,
    find_tool,
    operations,
    params_from_json,
)
from .queries import QUERIES

# Newest first; a client asking for another gets the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_INSTRUCTIONS = (
    "WGS Extract analyses a person's whole-genome BAM/CRAM/VCF on this "
    "computer. Paths must be absolute and inside the folders the user allowed "
    "(see read_manifest errors). Query tools return the result manifest: "
    "read `metrics` and `outputs` first. Job tools start a background job; "
    "follow it with job_status (wait_seconds) and stop it with cancel_job."
)


def _tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    *,
    read_only: bool,
    destructive: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "openWorldHint": False,
        },
    }


def _object(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_JOB_ID = {"type": "string", "description": "The job's id."}


class McpServer:
    def __init__(
        self,
        manager: JobManager,
        guard: PathGuard,
        *,
        allow_jobs: bool,
        stdout: TextIO,
    ) -> None:
        self.manager = manager
        self.guard = guard
        self.allow_jobs = allow_jobs
        self._out = stdout
        self._write_lock = threading.Lock()
        # Running tool calls by JSON-RPC request id, for notifications/cancelled.
        self._calls: dict[object, str] = {}
        self._cancelled: set[object] = set()
        self._threads: list[threading.Thread] = []
        self._calls_lock = threading.Lock()

    # Transport -------------------------------------------------------------

    def serve(self, stdin: TextIO) -> None:
        """Answer messages from ``stdin`` until it closes; then stop the jobs
        (the client has gone) and let the tool calls in progress end."""
        for line in stdin:
            if line.strip():
                self.handle_line(line)
        self.manager.shutdown()
        for thread in self._threads:
            thread.join()

    def _send(self, message: dict[str, Any]) -> None:
        text = json.dumps({"jsonrpc": "2.0", **message}, separators=(",", ":"))
        with self._write_lock:
            self._out.write(text + "\n")
            self._out.flush()

    def _reply(self, request_id: object, result: dict[str, Any]) -> None:
        self._send({"id": request_id, "result": result})

    def _error(self, request_id: object, code: int, message: str) -> None:
        self._send({"id": request_id, "error": {"code": code, "message": message}})

    def handle_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError as e:
            self._error(None, PARSE_ERROR, f"Parse error: {e}")
            return
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and (
                "result" in message or "error" in message
            ):
                return  # a response to a request we never send
            self._error(
                message.get("id") if isinstance(message, dict) else None,
                INVALID_REQUEST,
                "Invalid request.",
            )
            return
        method: str = message["method"]
        params = message.get("params") or {}
        if "id" not in message:
            if isinstance(params, dict):
                self._notification(method, params)
            return
        request_id = message["id"]
        if not isinstance(params, dict) or not isinstance(
            params.get("_meta", {}), dict
        ):
            self._error(request_id, INVALID_PARAMS, "params must be an object.")
            return
        if method == "tools/call":
            # Tools can take minutes, so each runs on its own thread.
            thread = threading.Thread(
                target=self._call_tool, args=(request_id, params), daemon=True
            )
            self._threads = [t for t in self._threads if t.is_alive()] + [thread]
            thread.start()
            return
        handler = self._methods().get(method)
        if handler is None:
            self._error(request_id, METHOD_NOT_FOUND, f"Unknown method {method!r}.")
            return
        try:
            self._reply(request_id, handler(params))
        except _InvalidParams as e:
            self._error(request_id, INVALID_PARAMS, str(e))
        except Exception as e:
            logging.exception("MCP request failed")
            self._error(request_id, -32603, f"Internal error: {e}")

    def _methods(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "initialize": self._initialize,
            "ping": lambda params: {},
            "tools/list": lambda params: {"tools": self.tools()},
        }

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/cancelled":
            if not isinstance(params.get("requestId"), (str, int)):
                return
            with self._calls_lock:
                job_id = self._calls.get(params.get("requestId"))
                if job_id:
                    self._cancelled.add(params.get("requestId"))
            if job_id:
                self.manager.cancel(job_id)

    # Methods ---------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "wgsextract", "version": __version__},
            "instructions": _INSTRUCTIONS,
        }

    def _offered(self, op: Operation) -> bool:
        if op.deletes and not self.guard.allow_destructive:
            return False
        return op.kind == "query" or self.allow_jobs

    def tools(self) -> list[dict[str, Any]]:
        read_only = not self.allow_jobs
        tools = [
            _tool(
                op.tool_name,
                f"{op.summary} (`wgsextract {op.command}`"
                + (
                    "; waits for the result)"
                    if op.kind == "query"
                    else "; starts a background job, follow it with job_status)"
                ),
                op.schema(read_only=read_only),
                read_only=read_only,
                destructive=op.may_delete and self.guard.allow_destructive,
            )
            for op in operations().values()
            if self._offered(op)
        ]
        tools += [
            _tool(query.name, query.summary, query.schema, read_only=True)
            for query in QUERIES.values()
        ]
        tools += [
            _tool(
                "list_jobs",
                "List this server's jobs: id, op, status and times.",
                _object({"status": {"type": "string"}}),
                read_only=True,
            ),
            _tool(
                "job_status",
                "A job's status, progress and, once finished, its result.",
                _object(
                    {
                        "job_id": _JOB_ID,
                        "wait_seconds": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": MAX_WAIT_SECONDS,
                            "description": "Wait up to this long for it to finish.",
                        },
                    },
                    "job_id",
                ),
                read_only=True,
            ),
        ]
        if self.allow_jobs:
            tools.append(
                _tool(
                    "cancel_job",
                    "Cancel a queued or running job.",
                    _object({"job_id": _JOB_ID}, "job_id"),
                    read_only=False,
                )
            )
        return tools

    def _call_tool(self, request_id: object, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        progress_token = (params.get("_meta") or {}).get("progressToken")
        try:
            if not isinstance(arguments, dict):
                raise _InvalidParams("arguments must be an object.")
            payload, failed = self._run_tool(
                request_id, str(name), arguments, progress_token
            )
        except _InvalidParams as e:
            self._error(request_id, INVALID_PARAMS, str(e))
            return
        except RequestError as e:
            payload, failed = e.to_dict(), True
        except Exception as e:
            logging.exception("MCP tool call failed")
            payload, failed = {"error": {"code": "internal", "message": str(e)}}, True
        finally:
            with self._calls_lock:
                self._calls.pop(request_id, None)
        with self._calls_lock:
            if request_id in self._cancelled:
                # The client gave up on it, so it expects no response.
                self._cancelled.discard(request_id)
                return
        self._reply(
            request_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(payload, separators=(",", ":"))}
                ],
                "structuredContent": payload,
                "isError": failed,
            },
        )

    def _run_tool(
        self,
        request_id: object,
        name: str,
        arguments: dict[str, Any],
        progress_token: object,
    ) -> tuple[dict[str, Any], bool]:
        op = find_tool(name)
        if op is not None and self._offered(op):
            params = params_from_json(op, arguments, read_only=not self.allow_jobs)
            self.guard.check(params)
            if not self.allow_jobs:
                arguments = {**arguments, **op.read_only_values()}
            job = self.manager.submit(op, arguments)
            if op.kind != "query":
                return job.view(), False
            with self._calls_lock:
                self._calls[request_id] = job.id
            job = self._follow(job, progress_token)
            return job.view(), job.status != "succeeded"
        if name in QUERIES:
            return QUERIES[name].run(self.guard, arguments), False
        if name == "list_jobs":
            status = arguments.get("status")
            return {
                "jobs": [
                    {"id": j.id, "op": j.op, "status": j.status, "created": j.created}
                    for j in self.manager.jobs()
                    if status is None or j.status == status
                ]
            }, False
        if name == "job_status":
            job = self._job(arguments)
            wait = arguments.get("wait_seconds") or 0
            if not isinstance(wait, (int, float)) or not 0 <= wait <= MAX_WAIT_SECONDS:
                raise RequestError(
                    400, "usage", f"wait_seconds must be 0 to {MAX_WAIT_SECONDS:g}."
                )
            if wait:
                job = self.manager.wait(job.id, float(wait)) or job
            return job.view(), False
        if name == "cancel_job" and self.allow_jobs:
            job = self._job(arguments)
            if job.done:
                raise RequestError(409, "finished", f"Job {job.id} already finished.")
            return (self.manager.cancel(job.id) or job).view(), False
        raise _InvalidParams(f"Unknown tool {name!r}.")

    def _follow(self, job: Job, progress_token: object) -> Job:
        """Wait for a job, relaying its events as progress notifications."""
        after = 0
        while True:
            events, finished = self.manager.events(job.id, after, 15.0)
            for event in events:
                after = event["seq"]
                if progress_token is not None:
                    text = event.get("message") or event.get("status") or ""
                    self._send(
                        {
                            "method": "notifications/progress",
                            "params": {
                                "progressToken": progress_token,
                                "progress": after,
                                "message": f"{event.get('stage', 'status')}: {text}",
                            },
                        }
                    )
            if finished:
                return self.manager.get(job.id) or job

    def _job(self, arguments: dict[str, Any]) -> Job:
        job_id = arguments.get("job_id")
        job = self.manager.get(str(job_id))
        if job is None:
            raise RequestError(404, "unknown_job", f"No job {job_id!r}.")
        return job


class _InvalidParams(Exception):
    pass


__all__ = ["McpServer", "PROTOCOL_VERSIONS", "find"]
