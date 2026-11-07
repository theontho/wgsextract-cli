"""`wgsextract serve --mcp-transport http` (#351): MCP over streamable HTTP,
spoken to with the official MCP SDK's client."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.fake_tools import python_tool
from tests.smoke_utils import get_cli_command
from tests.test_server import FAKE_DIR
from tests.test_server import fake_data as fake_data
from tests.test_server import work as work
from wgsextract_cli.core.last_run import state_directory

pytest.importorskip("mcp", reason="the optional MCP SDK is not installed")

import anyio  # noqa: E402
import httpx2  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@dataclass
class Served:
    proc: subprocess.Popen[str]
    url: str
    token: str
    discovery: Path


@contextlib.contextmanager
def _serve(*args: str) -> Iterator[Served]:
    proc = subprocess.Popen(
        [*get_cli_command(), "serve", "--mcp-transport", "http", "--ready-json", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, "serve exited"
        info = json.loads(line)
        discovery = state_directory() / "server" / "mcp.json"
        assert json.loads(discovery.read_text()) == info
        yield Served(proc, info["url"], info["token"], discovery)
    finally:
        if proc.poll() is None:
            proc.send_signal(
                signal.SIGINT if sys.platform != "win32" else signal.SIGTERM
            )
        proc.wait(60)


def _post(url: str, body: object, headers: dict[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers
    except urllib.error.HTTPError as e:
        return e.code, {
            **{k.lower(): v for k, v in e.headers.items()},
            "body": json.loads(e.read() or b"null"),
        }


@contextlib.asynccontextmanager
async def _session(served: Served):
    headers = {"Authorization": f"Bearer {served.token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=120) as http,
        streamable_http_client(served.url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def test_mcp_http_refuses_requests_without_the_token_or_a_local_host(work):
    with _serve("--allow", str(work)) as served:
        status, reply = _post(served.url, INITIALIZE, {})
        assert status == 401 and reply["body"]["error"]["code"] == "unauthorized"
        assert reply["www-authenticate"] == "Bearer"
        status, reply = _post(served.url, INITIALIZE, {"Authorization": "Bearer no"})
        assert status == 401

        auth = {"Authorization": f"Bearer {served.token}"}
        status, reply = _post(served.url, INITIALIZE, {**auth, "Host": "evil.test"})
        assert (status, reply["body"]["error"]["code"]) == (403, "bad_host")
        origin = {**auth, "Origin": "http://evil.test"}
        status, reply = _post(served.url, INITIALIZE, origin)
        assert (status, reply["body"]["error"]["code"]) == (403, "bad_origin")

        status, _ = _post(served.url, INITIALIZE, auth)
        assert status == 200


def test_mcp_http_listens_on_loopback_only_and_cleans_up(work):
    with _serve("--allow", str(work)) as served:
        assert served.url.startswith("http://127.0.0.1:")
        if sys.platform != "win32":
            assert served.discovery.stat().st_mode & 0o777 == 0o600
        process = psutil.Process(served.proc.pid)
        listening = []
        for proc in [process, *process.children(recursive=True)]:
            with contextlib.suppress(psutil.AccessDenied):
                listening += [
                    c.laddr
                    for c in proc.net_connections(kind="inet")
                    if c.status == psutil.CONN_LISTEN
                ]
        assert listening and {addr.ip for addr in listening} == {"127.0.0.1"}
    if sys.platform != "win32":
        assert served.proc.returncode == 0
        assert not served.discovery.exists()


def test_mcp_http_runs_tool_calls(work, fake_data):
    bam, _ = fake_data

    async def calls(served: Served) -> None:
        async with _session(served) as session:
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert {"info", "read_manifest", "job_status"} <= set(tools)
            assert "vcf_snp" not in tools
            assert tools["info"].annotations.read_only_hint is True

            messages: list[str] = []

            async def progress(step: float, total: float | None, message: str | None):
                messages.append(message or "")

            result = await session.call_tool(
                "info", {"input": str(bam)}, progress_callback=progress
            )
            assert result.is_error is False
            job = result.structured_content
            assert job["status"] == "succeeded" and job["result"]["metrics"]["sorted"]
            assert "status: succeeded" in messages

            result = await session.call_tool(
                "info", {"input": str(work.parent / "a.bam")}
            )
            assert result.is_error is True
            assert result.structured_content["error"]["code"] == "path_not_allowed"

    with _serve("--allow", str(work), "--allow", str(FAKE_DIR)) as served:
        anyio.run(calls, served)


HANGING_SAMTOOLS = """
import os, sys, time
open(os.path.join({pids!r}, str(os.getpid())), "w").close()
time.sleep(600)
"""


def _hanging_samtools(tmp_path, monkeypatch) -> Path:
    """Put a samtools that runs until killed first on PATH; each run records
    its pid in the returned folder."""
    if sys.platform == "win32":
        pytest.skip("a scripted fake can't replace a bare 'samtools' on Windows")
    pids = tmp_path / "pids"
    pids.mkdir()
    bindir = python_tool(
        tmp_path / "bin", "samtools", HANGING_SAMTOOLS.format(pids=str(pids))
    ).parent
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return pids


async def _all_cancelled(session: Any, pids: Path) -> None:
    """Wait until every job is cancelled and every samtools has exited."""
    with anyio.fail_after(60):
        while True:
            jobs = await session.call_tool("list_jobs", {})
            statuses = {j["status"] for j in jobs.structured_content["jobs"]}
            if statuses <= {"cancelled"}:
                break
            await anyio.sleep(0.2)
    with anyio.fail_after(30):
        for pid in pids.iterdir():
            while True:
                try:
                    tool = psutil.Process(int(pid.name))
                    if tool.status() == psutil.STATUS_ZOMBIE:
                        break
                except psutil.NoSuchProcess:
                    break
                await anyio.sleep(0.2)


def test_cancelling_an_mcp_http_call_stops_its_job(
    tmp_path, work, fake_data, monkeypatch
):
    bam, _ = fake_data
    pids = _hanging_samtools(tmp_path, monkeypatch)

    async def cancel(served: Served) -> None:
        async with _session(served) as session:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(session.call_tool, "info", {"input": str(bam)})
                with anyio.fail_after(120):
                    while not any(pids.iterdir()):
                        await anyio.sleep(0.2)
                tasks.cancel_scope.cancel()
            await _all_cancelled(session, pids)
            jobs = await session.call_tool("list_jobs", {})
            assert [j["status"] for j in jobs.structured_content["jobs"]] == [
                "cancelled"
            ]

    with _serve("--allow", str(work), "--allow", str(FAKE_DIR)) as served:
        anyio.run(cancel, served)


def test_mcp_http_without_the_sdk_says_how_to_get_it(work):
    # The optional SDK missing from the environment (the OS boundary).
    code = (
        "import sys; sys.modules['mcp'] = None; "
        "from wgsextract_cli.main import main; "
        f"sys.argv = ['wgsextract', 'serve', '--mcp-transport', 'http', "
        f"'--allow', {str(work)!r}]; main()"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 3
    assert "wgsextract-cli[mcp]" in proc.stderr
