"""``wgsextract serve``: the typed API as a local HTTP/JSON server, or as an MCP
server on stdio (``--mcp``). See docs/server.md."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from wgsextract_cli.core.config import get_config_path, settings
from wgsextract_cli.core.results import json_mode
from wgsextract_cli.core.utils import UsageError

if TYPE_CHECKING:
    from wgsextract_cli.server.jobs import JobManager
    from wgsextract_cli.server.operations import PathGuard


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "serve",
        parents=[base_parser],
        help="Serve the WGS Extract API to local apps and AI agents, over "
        "HTTP/JSON or MCP.",
        description="Run the same operations as the CLI (info, vcf snp, ...) "
        "for other programs on this computer: as an HTTP/JSON API on "
        "127.0.0.1 with a per-launch token, or as an MCP server on stdio "
        "(--mcp). Operations run as background jobs with progress, cancelling "
        "and result manifests. Requests may only name files inside the "
        "folders you allow: --allow, config server_allowed_paths, "
        "genome_library, reference_library and output_directory.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Port on 127.0.0.1 (default: config server_port, else any free port).",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="DIR",
        help="Also let requests read and write inside DIR (repeatable).",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        help="Long jobs that run at once; others wait in a queue (default: "
        "config server_max_jobs, else 1). Quick queries never wait.",
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help="Speak the Model Context Protocol on stdin/stdout instead of HTTP.",
    )
    parser.add_argument(
        "--allow-jobs",
        action="store_true",
        help="With --mcp, also offer the operations that write files or change "
        "a sample's settings, not just the read-only queries.",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Also run requests that delete data a command didn't make: "
        "bam unindex, sample unset and ref gene-map --delete.",
    )
    parser.add_argument(
        "--openapi",
        action="store_true",
        help="Print the HTTP API's OpenAPI document and exit.",
    )
    parser.set_defaults(func=run)


def allowed_roots(extra: Iterable[str]) -> list[Path]:
    """The folders requests may use: ``extra`` and server_allowed_paths, which
    must exist, and the configured data folders that do (one not created or
    mounted yet doesn't stop the server)."""
    configured = settings.get("server_allowed_paths") or []
    if isinstance(configured, str):
        configured = [configured]
    roots = [Path(os.path.expanduser(root)) for root in (*extra, *configured)]
    for root in roots:
        if not root.is_dir():
            raise UsageError(f"Allowed folder {root} doesn't exist.")
    for key in ("genome_library", "reference_library", "output_directory"):
        if not settings.get(key):
            continue
        root = Path(os.path.expanduser(str(settings[key])))
        if root.is_dir():
            roots.append(root)
        else:
            logging.warning(f"Not allowing {key} {root}: it doesn't exist.")
    return roots


def _setting_int(
    value: int | None, flag: str, key: str, low: int, high: int | None = None
) -> int:
    if value is None:
        value = int(settings.get(key) or low)
    if value < low or (high is not None and value > high):
        raise UsageError(f"{flag} (config {key}) is out of range: {value}.")
    return value


def run(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.last_run import state_directory
    from wgsextract_cli.server.jobs import JobManager

    if args.openapi:
        import json

        from wgsextract_cli.server.http_api import openapi_document

        sys.stdout.write(json.dumps(openapi_document(), indent=1) + "\n")
        return
    if args.allow_jobs and not args.mcp:
        raise UsageError("--allow-jobs is for --mcp; HTTP clients may run any job.")
    if args.mcp and json_mode():
        raise UsageError("--mcp uses stdout for the protocol; drop --json.")
    port = _setting_int(args.port, "--port", "server_port", 0, 65535)
    max_jobs = _setting_int(args.max_jobs, "--max-jobs", "server_max_jobs", 1)

    from wgsextract_cli.server.operations import PathGuard

    guard = PathGuard(
        allowed_roots(args.allow), allow_destructive=args.allow_destructive
    )
    if not guard.roots:
        logging.warning(
            "No folders are allowed, so requests can't name any file. Pass "
            "--allow DIR or set server_allowed_paths in config.toml."
        )
    state = state_directory() / "server"
    # The HTTP server keeps its jobs across restarts. Each MCP session (one per
    # agent conversation, several may run at once) has its own, removed when
    # it ends: the outputs and their manifests stay where the jobs saved them.
    jobs = (
        Path(tempfile.mkdtemp(prefix="mcp-", dir=_made(state)))
        if args.mcp
        else state / "jobs"
    )
    manager = JobManager(
        jobs,
        config_path=get_config_path(),
        max_jobs=max_jobs,
        debug=bool(getattr(args, "debug", False)),
        default_outdir=settings.get("output_directory"),
        allowed=guard.roots,
    )
    try:
        if args.mcp:
            _serve_mcp(manager, guard, allow_jobs=args.allow_jobs)
        else:
            _serve_http(manager, guard, port, state / "server.json")
    finally:
        manager.shutdown()
        if args.mcp:
            shutil.rmtree(jobs, ignore_errors=True)


def _made(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _serve_mcp(manager: JobManager, guard: PathGuard, *, allow_jobs: bool) -> None:
    from wgsextract_cli.server.mcp_server import McpServer

    logging.info(
        "WGS Extract MCP server on stdio"
        + (" (jobs allowed)" if allow_jobs else " (read-only queries)")
    )
    server = McpServer(manager, guard, allow_jobs=allow_jobs, stdout=sys.stdout)
    try:
        server.serve(sys.stdin)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Stopping the MCP server.")


def _serve_http(
    manager: JobManager, guard: PathGuard, port: int, discovery: Path
) -> None:
    from wgsextract_cli.server.http_api import Server
    from wgsextract_cli.server.jobs import write_private_json

    token = secrets.token_urlsafe(32)
    try:
        server = Server(port, token, manager, guard)
    except OSError as e:
        raise UsageError(f"Can't listen on 127.0.0.1:{port}: {e}") from None
    write_private_json(
        discovery,
        {"url": server.url + "/v1", "token": token, "pid": os.getpid()},
    )
    print(
        f"WGS Extract server on {server.url}/v1\n"
        f"Token: {token}\n"
        f"  (also in {discovery}, readable by you only)\n"
        f"Allowed folders: {', '.join(map(str, guard.roots)) or 'none'}\n"
        f"Try: curl -H 'Authorization: Bearer {token}' {server.url}/v1/ops\n"
        "Stop with Ctrl-C.",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Stopping the server.")
    finally:
        server.server_close()
        _remove_discovery(discovery)


def _remove_discovery(discovery: Path) -> None:
    """Remove the discovery file unless a newer server has replaced it."""
    import json

    try:
        if json.loads(discovery.read_text()).get("pid") == os.getpid():
            discovery.unlink()
    except (OSError, ValueError):
        pass
