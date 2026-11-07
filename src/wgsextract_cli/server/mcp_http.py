"""MCP over streamable HTTP (``wgsextract serve --mcp-transport http``).

The official MCP Python SDK (the optional ``mcp`` package) speaks the
transport; the tools are :class:`~.mcp_server.McpTools`, as on stdio. The
endpoint is ``/mcp`` on 127.0.0.1 only, and every request needs the
per-launch bearer token and a local Host and Origin, as the HTTP API does.
"""

from __future__ import annotations

import functools
import logging
import socket
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from wgsextract_cli import __version__
from wgsextract_cli.core.utils import MissingDependencyError

from .http_api import MAX_BODY_BYTES, check_headers, local_hosts
from .mcp_server import INSTRUCTIONS, InvalidParams, McpTools
from .operations import RequestError

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

PATH = "/mcp"


SDK_RANGE = ">=2.3,<3"


def require_sdk() -> None:
    """Stop with a clear error unless a supported MCP SDK is installed."""
    try:
        # The 2.x interfaces build_app uses; 1.x lacks them.
        import mcp.server.context  # noqa: F401
        import mcp.server.lowlevel  # noqa: F401
        import mcp.server.transport_security  # noqa: F401
        import mcp.shared.exceptions  # noqa: F401
        import mcp_types  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as e:
        raise MissingDependencyError(
            f"MCP over HTTP needs the optional 'mcp' package {SDK_RANGE} (the "
            "official MCP Python SDK), which is not installed, or too old, for "
            f"{sys.executable}. Install it into that environment (pip install "
            "'wgsextract-cli[mcp]', or pixi run -e mcp from a source checkout), "
            "or use MCP over stdio (--mcp-transport stdio), which needs nothing "
            f"extra. ({e})"
        ) from e


def listen(port: int) -> socket.socket:
    """A socket listening on 127.0.0.1:``port`` (0: any free port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


class _Guard:
    """ASGI middleware refusing requests without the token, or naming another
    host or origin, before the SDK sees them."""

    def __init__(self, app: ASGIApp, token: str, hosts: frozenset[str]) -> None:
        self.app = app
        self.token = token
        self.hosts = hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            from starlette.datastructures import Headers
            from starlette.responses import JSONResponse

            headers = Headers(scope=scope)
            try:
                check_headers(headers.get, self.hosts, self.token, "mcp.json")
            except RequestError as e:
                extra = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {}
                response = JSONResponse(
                    e.to_dict(),
                    e.status,
                    headers={"Cache-Control": "no-store", **extra},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(tools: McpTools, token: str, port: int) -> ASGIApp:
    """The ASGI app serving ``tools`` at ``/mcp`` to clients with ``token``."""
    import anyio
    import anyio.from_thread
    import anyio.to_thread
    import mcp_types as types
    from mcp.server.context import ServerRequestContext
    from mcp.server.lowlevel import Server
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.shared.exceptions import MCPError

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(tool) for tool in tools.list()]
        )

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        jobs: list[str] = []
        cancelled = False
        lock = threading.Lock()

        def started(job_id: str) -> None:
            # A job submitted after the call was cancelled is cancelled at once.
            with lock:
                jobs.append(job_id)
                late = cancelled
            if late:
                tools.manager.cancel(job_id)

        def progress(step: int, message: str) -> None:
            try:
                anyio.from_thread.run(
                    functools.partial(ctx.session.report_progress, step, None, message)
                )
            except Exception:  # the call was cancelled or the client left
                logging.debug("MCP progress not sent", exc_info=True)

        run: Callable[[], dict[str, Any]] = functools.partial(
            tools.call,
            params.name,
            params.arguments or {},
            progress=progress,
            started=started,
        )
        try:
            # Tools can take minutes; the job keeps the thread, not the loop.
            result = await anyio.to_thread.run_sync(run, abandon_on_cancel=True)
        except InvalidParams as e:
            raise MCPError(types.INVALID_PARAMS, str(e)) from None
        except anyio.get_cancelled_exc_class():
            # notifications/cancelled, or the client went away.
            with lock:
                cancelled = True
                submitted = list(jobs)
            for job_id in submitted:
                tools.manager.cancel(job_id)
            raise
        return types.CallToolResult.model_validate(result)

    server: Server[Any] = Server(
        "wgsextract",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    hosts = local_hosts(port)
    app = server.streamable_http_app(
        streamable_http_path=PATH,
        max_request_body_size=MAX_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(hosts),
            allowed_origins=[f"http://{host}" for host in sorted(hosts)],
        ),
    )
    return _Guard(app, token, hosts)


def serve(tools: McpTools, token: str, sock: socket.socket) -> None:
    """Serve until interrupted: Ctrl-C or SIGTERM stops it gracefully, then
    is raised again."""
    import uvicorn

    port = sock.getsockname()[1]
    config = uvicorn.Config(
        build_app(tools, token, port),
        lifespan="on",
        log_level="warning",
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=5,
    )
    uvicorn.Server(config).run(sockets=[sock])
