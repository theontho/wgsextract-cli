"""`wgsextract serve` (#351): the typed API over local HTTP/JSON and MCP."""

import contextlib
import http.client
import json
import ntpath
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import psutil
import pytest
from jsonschema import Draft202012Validator

from tests.fake_tools import python_tool
from tests.smoke_utils import ensure_fake_data, get_cli_command
from wgsextract_cli.core import results
from wgsextract_cli.core.config import get_config_path
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.server.http_api import Server
from wgsextract_cli.server.jobs import JobManager
from wgsextract_cli.server.operations import PathGuard, RequestError

CLI_ROOT = Path(__file__).resolve().parents[1]
FAKE_DIR = CLI_ROOT / "out" / "fake_30x"
VALIDATOR = Draft202012Validator(results.load_schema())
TOKEN = "test-token"


@pytest.fixture
def fake_data() -> tuple[Path, Path]:
    if not shutil.which("samtools") or not shutil.which("bcftools"):
        pytest.skip("samtools/bcftools not found in PATH")
    ensure_fake_data(str(FAKE_DIR))
    return FAKE_DIR / "fake.bam", FAKE_DIR / "fake_ref.fa"


class Client:
    def __init__(self, url: str) -> None:
        self.url = url + "/v1"

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        token: str | None = TOKEN,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, _decode(response)
        except urllib.error.HTTPError as e:
            return e.code, _decode(e)

    def events(self, job_id: str) -> list[dict[str, Any]]:
        """A job's server-sent events, read until the stream ends."""
        request = urllib.request.Request(f"{self.url}/jobs/{job_id}/events")
        request.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(request, timeout=300) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            text = response.read().decode()
        return [
            json.loads(line.removeprefix("data: "))
            for line in text.splitlines()
            if line.startswith("data: ")
        ]


def _decode(response: Any) -> Any:
    text = response.read().decode()
    if response.headers.get("Content-Type") == "application/json":
        return json.loads(text)
    return text


@pytest.fixture
def work(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


@contextlib.contextmanager
def _serve(tmp_path: Path, roots: list[Path], max_jobs: int = 1) -> Iterator[Client]:
    manager = JobManager(
        tmp_path / "state" / "jobs", config_path=get_config_path(), max_jobs=max_jobs
    )
    server = Server(0, TOKEN, manager, PathGuard(roots))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(server.url)
    finally:
        server.shutdown()
        server.server_close()
        manager.shutdown()


@pytest.fixture
def client(tmp_path: Path, work: Path) -> Iterator[Client]:
    with _serve(tmp_path, [work, FAKE_DIR]) as client:
        yield client


def test_requests_need_the_token_and_a_local_host(client):
    assert client.request("GET", "/health", token=None)[0] == 200
    for token in (None, "wrong"):
        status, body = client.request("GET", "/ops", token=token)
        assert (status, body["error"]["code"]) == (401, "unauthorized")
    status, body = client.request("GET", "/ops", headers={"Host": "evil.example"})
    assert (status, body["error"]["code"]) == (403, "bad_host")
    status, body = client.request(
        "GET", "/ops", headers={"Origin": "https://evil.example"}
    )
    assert (status, body["error"]["code"]) == (403, "bad_origin")


def test_bad_lengths_are_refused_and_errors_close_the_connection(client):
    # PR #422 review: a negative Content-Length read the body to EOF, and an
    # error left an unread body to be parsed as the next request.
    host, port = client.url.removeprefix("http://").removesuffix("/v1").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=30)
    connection.putrequest("POST", "/v1/ops/info")
    connection.putheader("Authorization", "Bearer " + TOKEN)
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", "-1")
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 400
    assert json.loads(response.read())["error"]["code"] == "bad_length"
    assert response.headers["Connection"] == "close"
    connection.close()


def test_jobs_writing_to_one_folder_run_one_at_a_time(
    tmp_path, work, fake_data, slow_coverage
):
    # PR #422 review: with --max-jobs 2, two jobs saving the same fixed file
    # names in one folder ran at once and overwrote each other's outputs.
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    params = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        first = client.request("POST", "/ops/info/calculate-coverage", params)[1]
        same = client.request("POST", "/ops/info/calculate-coverage", params)[1]
        apart = client.request(
            "POST", "/ops/info/calculate-coverage", {**params, "outdir": str(other)}
        )[1]
        assert (first["status"], same["status"], apart["status"]) == (
            "running",
            "queued",
            "running",
        )
        with pytest.raises(WGSExtractError, match="Another wgsextract server"):
            JobManager(tmp_path / "state" / "jobs", config_path=None)


def test_operations_and_openapi_follow_the_typed_api(client):
    status, body = client.request("GET", "/ops")
    ops = {op["op"]: op["kind"] for op in body["ops"]}
    assert status == 200
    assert ops["info"] == "query" and ops["vcf/snp"] == "job"
    status, body = client.request("GET", "/ops/vcf/snp")
    assert {"input", "ref", "region", "ploidy"} <= set(body["params"]["properties"])
    assert body["params"]["required"] == ["input"]
    # Settings that name a program to run stay out of reach of requests.
    _, body = client.request("GET", "/ops/lineage/mt-haplogroup")
    assert "haplogrep_path" not in body["params"]["properties"]

    status, document = client.request("GET", "/openapi.json")
    assert status == 200 and document["openapi"].startswith("3.1")
    assert {f"/v1/ops/{op}" for op in ops} <= set(document["paths"])
    run = document["paths"]["/v1/ops/vcf/snp"]["post"]
    schema = run["requestBody"]["content"]["application/json"]["schema"]
    assert document["components"]["schemas"][schema["$ref"].split("/")[-1]][
        "properties"
    ]["region"]


def test_vcf_snp_runs_as_a_job_with_progress_and_a_manifest(client, work, fake_data):
    bam, ref = fake_data
    params = {
        "input": str(bam),
        "ref": str(ref),
        "outdir": str(work),
        "region": "chrM",
        "ploidy": "1",
    }
    status, job = client.request("POST", "/ops/vcf/snp", params)
    assert status == 202 and job["status"] in ("queued", "running")

    events = client.events(job["id"])
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert statuses[0] == "queued" and statuses[-1] == "succeeded"
    assert any(e["type"] == "progress" for e in events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))

    status, job = client.request("GET", f"/jobs/{job['id']}")
    assert (status, job["status"], job["exit_code"]) == (200, "succeeded", 0)
    vcf = work / "snps.vcf.gz"
    assert str(vcf) in [o["path"] for o in job["result"]["outputs"]]
    calls = subprocess.run(
        ["bcftools", "view", "-H", str(vcf)], capture_output=True, text=True
    )
    assert calls.returncode == 0 and calls.stdout.startswith("chrM")
    manifest = json.loads(results.manifest_path_for(vcf).read_text())
    VALIDATOR.validate(manifest)
    assert manifest["status"] == "ok"
    # Compact by default; provenance with full=1.
    assert "provenance" not in job["result"]
    _, full = client.request("GET", f"/jobs/{job['id']}?full=1")
    assert full["result"]["provenance"]["params"]["region"] == "chrM"


def test_info_query_waits_and_reports_failures_as_failed_jobs(client, work, fake_data):
    bam, _ = fake_data
    status, job = client.request(
        "POST", "/ops/info?wait=120", {"input": str(bam), "outdir": str(work)}
    )
    assert (status, job["status"]) == (200, "succeeded")
    assert job["result"]["metrics"]["sorted"] is True

    status, job = client.request(
        "POST", "/ops/info?wait=120", {"input": str(work / "missing.bam")}
    )
    assert (status, job["status"], job["exit_code"]) == (200, "failed", 1)
    assert "missing.bam" in job["error"]["message"]


@pytest.mark.parametrize(
    ("op", "params", "status", "code"),
    [
        ("info", {"input": "{outside}/a.bam"}, 403, "path_not_allowed"),
        ("info", {"input": "{work}/../outside/a.bam"}, 403, "path_not_allowed"),
        ("info", {"input": "{work}/escape/a.bam"}, 403, "path_not_allowed"),
        ("info", {"input": "a.bam"}, 400, "usage"),
        # Security review of PR #422: Windows opens UNC paths over SMB.
        ("info", {"input": "//attacker/share/a.bam"}, 403, "path_not_allowed"),
        ("info", {"input": "\\\\attacker\\share\\a.bam"}, 403, "path_not_allowed"),
        ("info", {"input": "{work}/a.bam", "bogus": 1}, 400, "usage"),
        ("vcf/snp", {"input": "{work}/a.bam", "threads": "four"}, 400, "usage"),
        (
            "lineage/mt-haplogroup",
            {"input": "{work}/a.bam", "haplogrep_path": "/bin/sh"},
            400,
            "usage",
        ),
        ("nope", {}, 404, "unknown_op"),
        # PR #422 review: these were 500 "internal" errors, or jobs with
        # values the schema forbids.
        ("vcf/snp", {}, 400, "usage"),
        (
            "lineage/y-haplogroup",
            {"input": "{work}/a.bam", "min_reads": None},
            400,
            "usage",
        ),
    ],
)
def test_requests_are_validated_and_confined_to_allowed_folders(
    client, tmp_path, work, op, params, status, code
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if hasattr(os, "symlink"):
        try:
            (work / "escape").symlink_to(outside, target_is_directory=True)
        except OSError:
            pass
    if "escape" in str(params) and not (work / "escape").is_symlink():
        pytest.skip("can't create symlinks here")
    body = {
        k: v.format(work=work, outside=outside) if isinstance(v, str) else v
        for k, v in params.items()
    }
    got, response = client.request("POST", f"/ops/{op}", body)
    assert (got, response["error"]["code"]) == (status, code)
    assert client.request("GET", "/jobs")[1]["jobs"] == []


class _RecordingFlavour:
    """Windows path rules on any OS, recording what would touch the disk."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(ntpath, name)

    def abspath(self, path: str) -> str:
        assert ntpath.isabs(path), path  # else it would read the working folder
        return ntpath.normpath(path)

    def realpath(self, path: str) -> str:
        self.opened.append(path)
        return ntpath.normpath(path)


@pytest.mark.parametrize(
    "path",
    [
        r"\\attacker\share\a.bam",
        "//attacker/share/a.bam",
        r"\/attacker/share/a.bam",
        r"\\?\UNC\attacker\share\a.bam",
        r"\\?\C:\data\a.bam",
        r"\\.\pipe\a.bam",
        r"\??\UNC\attacker\share\a.bam",
        r"C:\data\..\other\a.bam",
    ],
)
def test_path_guard_refuses_network_paths_without_opening_them(path):
    # Security review of PR #422: on Windows, checking `\\attacker\share\a.bam`
    # resolved it first, which connects to the host and sends the user's NTLM
    # credentials, before the 403.
    flavour = _RecordingFlavour()
    guard = PathGuard([r"C:\data"], pathmod=flavour)
    assert flavour.opened == [r"C:\data"]
    with pytest.raises(RequestError) as refused:
        guard.check_path("input", path)
    assert (refused.value.status, refused.value.code) == (403, "path_not_allowed")
    assert flavour.opened == [r"C:\data"]
    guard.check_path("input", "c:/DATA/sub/a.bam")
    assert flavour.opened[-1] == "c:/DATA/sub/a.bam"


def test_read_only_mcp_info_saves_nothing(tmp_path, fake_data):
    # Security review of PR #422: read-only `info` created the folders named
    # by outdir, and saved its metrics cache next to the input.
    data = tmp_path / "data"
    data.mkdir()
    for name in ("fake.bam", "fake.bam.bai"):
        shutil.copy2(FAKE_DIR / name, data / name)
    before = {p: p.read_bytes() if p.is_file() else None for p in data.rglob("*")}

    mcp = Mcp("--allow", str(data))
    schema = next(
        t["inputSchema"]
        for t in mcp.request("tools/list", {})["result"]["tools"]
        if t["name"] == "info"
    )
    assert not {"outdir", "detailed", "cache"} & set(schema["properties"])
    bam = str(data / "fake.bam")
    result = mcp.call("info", input=bam)
    assert result["structuredContent"]["result"]["metrics"]["sorted"] is True
    assert mcp.call("info", input=bam, cache=False)["isError"] is False
    for refused in (
        {"outdir": str(data / "a" / "b")},
        {"detailed": True},
        {"cache": True},
    ):
        result = mcp.call("info", input=bam, **refused)
        assert result["structuredContent"]["error"]["code"] == "read_only", refused
    assert mcp.close() == 0

    after = {p: p.read_bytes() if p.is_file() else None for p in data.rglob("*")}
    assert after == before


SLOW_SAMTOOLS = """
import os, subprocess, sys, time
if sys.argv[1:2] == ["coverage"]:
    open(os.path.join({pids!r}, str(os.getpid())), "w").close()
    time.sleep(600)
sys.exit(subprocess.run([{real!r}, *sys.argv[1:]]).returncode)
"""


@pytest.fixture
def slow_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_data) -> Path:
    """`samtools coverage` hangs (a very slow tool); other subcommands are real.
    Returns the folder where each hanging call records its pid."""
    if sys.platform == "win32":
        pytest.skip(
            "Windows starts a bare 'samtools' with CreateProcess, which finds "
            "only .exe files, so a scripted fake can't replace it"
        )
    pids = tmp_path / "pids"
    pids.mkdir()
    real = shutil.which("samtools")
    source = SLOW_SAMTOOLS.format(pids=str(pids), real=real)
    bindir = python_tool(tmp_path / "bin", "samtools", source).parent
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return pids


def _job_logs(tmp_path: Path) -> str:
    """The workers' logs, to show why a job got stuck."""
    return "\n".join(
        f"== {log.name}\n{log.read_text(errors='replace')}"
        for log in sorted((tmp_path / "state" / "jobs").glob("*.log"))
    )


def _wait_for(predicate: Any, tmp_path: Path, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out\n{_job_logs(tmp_path)}"
        time.sleep(0.2)


def test_cancel_stops_running_jobs_and_drops_queued_ones(
    tmp_path, client, work, fake_data, slow_coverage
):
    bam, ref = fake_data
    params = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
    _, running = client.request("POST", "/ops/info/calculate-coverage", params)
    _, queued = client.request("POST", "/ops/vcf/snp", {**params, "region": "chrM"})
    assert queued["status"] == "queued"

    status, cancelled = client.request("DELETE", f"/jobs/{queued['id']}")
    assert (status, cancelled["status"], cancelled["exit_code"]) == (
        200,
        "cancelled",
        130,
    )

    _wait_for(lambda: any(slow_coverage.iterdir()), tmp_path)
    tool_pid = int(next(slow_coverage.iterdir()).name)
    status, job = client.request("DELETE", f"/jobs/{running['id']}")
    assert status == 202 and job["cancel_requested"]
    _, job = client.request("GET", f"/jobs/{running['id']}?wait=60")
    assert (job["status"], job["exit_code"]) == ("cancelled", 130)
    assert job["result"]["status"] == "cancelled"
    assert not psutil.pid_exists(tool_pid) or (
        psutil.Process(tool_pid).status() == psutil.STATUS_ZOMBIE
    )
    status, body = client.request("DELETE", f"/jobs/{running['id']}")
    assert (status, body["error"]["code"]) == (409, "finished")
    assert not (work / "snps.vcf.gz").exists()


def test_jobs_outlive_the_server_and_can_be_rerun(
    tmp_path, work, fake_data, slow_coverage
):
    bam, ref = fake_data
    params = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
    with _serve(tmp_path, [work, FAKE_DIR]) as client:
        _, done = client.request(
            "POST", "/ops/info?wait=100", {"input": str(bam), "outdir": str(work)}
        )
        assert done["status"] == "succeeded", _job_logs(tmp_path)
        _, running = client.request("POST", "/ops/info/calculate-coverage", params)
        _wait_for(lambda: any(slow_coverage.iterdir()), tmp_path)
    # The server stopped, as on Ctrl-C, with the coverage job still running.

    with _serve(tmp_path, [work, FAKE_DIR]) as client:
        _, listed = client.request("GET", "/jobs")
        statuses = {job["id"]: job["status"] for job in listed["jobs"]}
        assert statuses == {done["id"]: "succeeded", running["id"]: "interrupted"}
        _, job = client.request("GET", f"/jobs/{done['id']}")
        assert job["result"]["metrics"]["sorted"] is True

        status, rerun = client.request("POST", f"/jobs/{running['id']}/rerun")
        assert status == 202 and rerun["id"] != running["id"]
        _, rerun = client.request("GET", f"/jobs/{rerun['id']}?wait=1")
        assert rerun["status"] == "running"


def test_serve_command_publishes_its_address_and_cleans_up(tmp_path, work):
    proc = subprocess.Popen(
        [*get_cli_command(), "serve", "--allow", str(work)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    discovery = tmp_path / "home" / "missing"
    try:
        assert proc.stderr is not None
        banner = ""
        while "Stop with Ctrl-C" not in banner:
            line = proc.stderr.readline()
            assert line, f"serve exited: {banner}"
            banner += line
        discovery = Path(banner.split("(also in ")[1].split(",")[0])
        info = json.loads(discovery.read_text())
        assert info["url"] in banner and info["token"] in banner
        if sys.platform != "win32":
            assert discovery.stat().st_mode & 0o777 == 0o600
        client = Client(info["url"].removesuffix("/v1"))
        status, index = client.request("GET", "", token=info["token"])
        assert status == 200 and str(work.resolve()) in index["allowed_paths"]
    finally:
        proc.send_signal(signal.SIGINT if sys.platform != "win32" else signal.SIGTERM)
        proc.wait(30)
    if sys.platform != "win32":
        assert proc.returncode == 0
        assert not discovery.exists()


class Mcp:
    """A `wgsextract serve --mcp` subprocess, spoken to line by line."""

    def __init__(self, *args: str) -> None:
        self.proc = subprocess.Popen(
            [*get_cli_command(), "serve", "--mcp", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.messages: Queue[dict[str, Any]] = Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        reply = self.request(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}
        )
        assert reply["result"]["protocolVersion"] == "2025-06-18"
        self.send({"method": "notifications/initialized"})

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.messages.put(json.loads(line))

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Any) -> dict[str, Any]:
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        while True:
            try:
                message = self.messages.get(timeout=300)
            except Empty:
                raise AssertionError(f"no reply to {method}") from None
            if message.get("id") == self.next_id:
                return message

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        return reply.get("result") or reply

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(60)


def test_mcp_offers_read_only_queries_by_default(work, fake_data):
    bam, _ = fake_data
    mcp = Mcp("--allow", str(work), "--allow", str(FAKE_DIR))
    tools = {t["name"]: t for t in mcp.request("tools/list", {})["result"]["tools"]}
    assert {"info", "read_manifest", "job_status"} <= set(tools)
    assert "vcf_snp" not in tools and "cancel_job" not in tools
    assert tools["info"]["annotations"]["readOnlyHint"] is True
    assert tools["info"]["inputSchema"]["required"] == ["input"]

    result = mcp.call("info", input=str(bam))
    assert result["isError"] is False
    job = result["structuredContent"]
    assert job["status"] == "succeeded" and job["result"]["metrics"]["sorted"]
    assert json.loads(result["content"][0]["text"]) == job

    result = mcp.call("info", input=str(work.parent / "a.bam"))
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "path_not_allowed"
    assert mcp.call("vcf_snp", input=str(bam))["error"]["code"] == -32602

    # PR #422 review: info's table_format saves files, yet read-only MCP
    # offered it; malformed params killed the call or the server.
    assert "table_format" not in tools["info"]["inputSchema"]["properties"]
    result = mcp.call("info", input=str(bam), table_format="tsv")
    assert result["structuredContent"]["error"]["code"] == "read_only"
    assert not list(work.glob("*.tsv"))
    assert mcp.request("tools/call", [])["error"]["code"] == -32602
    mcp.send({"method": "notifications/cancelled", "params": {"requestId": {}}})
    assert mcp.request("ping", {})["result"] == {}
    assert mcp.close() == 0


def test_mcp_runs_jobs_when_allowed(work, fake_data):
    bam, ref = fake_data
    mcp = Mcp("--allow", str(work), "--allow", str(FAKE_DIR), "--allow-jobs")
    started = mcp.call(
        "vcf_snp",
        input=str(bam),
        ref=str(ref),
        outdir=str(work),
        region="chrM",
        ploidy="1",
    )["structuredContent"]
    assert started["status"] in ("queued", "running")
    job = mcp.call("job_status", job_id=started["id"], wait_seconds=300)
    assert job["structuredContent"]["status"] == "succeeded"

    manifest = mcp.call("read_manifest", path=str(work / "snps.vcf.manifest.json"))
    assert manifest["structuredContent"]["command"] == "vcf snp"
    assert "provenance" not in manifest["structuredContent"]
    assert mcp.call("cancel_job", job_id=started["id"])["isError"] is True
    assert mcp.close() == 0
