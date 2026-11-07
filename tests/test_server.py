"""`wgsextract serve` (#351): the typed API over local HTTP/JSON and MCP."""

import contextlib
import gzip
import http.client
import importlib.util
import json
import ntpath
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import typing
import urllib.error
import urllib.parse
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
from wgsextract_cli.api._params import json_schema
from wgsextract_cli.core import results
from wgsextract_cli.core.config import get_config_path, settings
from wgsextract_cli.core.utils import UsageError, WGSExtractError
from wgsextract_cli.server.http_api import Server
from wgsextract_cli.server.jobs import JobManager
from wgsextract_cli.server.operations import (
    PathGuard,
    RequestError,
    find,
    operations,
    params_from_json,
)

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
def _serve(
    tmp_path: Path,
    roots: list[Path],
    max_jobs: int = 1,
    *,
    allow_destructive: bool = False,
) -> Iterator[Client]:
    guard = PathGuard(roots, allow_destructive=allow_destructive)
    manager = JobManager(
        tmp_path / "state" / "jobs",
        config_path=get_config_path(),
        max_jobs=max_jobs,
        allowed=roots,
    )
    server = Server(0, TOKEN, manager, guard)
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


@pytest.mark.parametrize(
    ("op", "into_work"),
    [
        (
            "bam/merge",
            lambda work, bam: {
                "inputs": [str(bam), str(bam)],
                "output": str(work / "m.bam"),
            },
        ),
        ("qc/multiqc", lambda work, bam: {"input": str(work)}),
    ],
    ids=["named-output", "folder-input"],
)
def test_a_job_writing_into_a_busy_folder_waits(
    tmp_path, work, fake_data, slow_coverage, op, into_work
):
    # PR #445 review: only the outdir was serialised, so a job that writes into
    # a busy folder by an output path or a folder input ran at the same time.
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request(
            "POST", f"/ops/{op}", {**into_work(work, bam), "outdir": str(other)}
        )
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


def test_a_home_relative_folder_input_locks_the_folder_it_names(
    tmp_path, work, fake_data, slow_coverage, monkeypatch
):
    # PR #458 re-review: the lock read `~/reports` unexpanded, so it locked
    # the folder above, not the reports folder the job writes into.
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(work.parent))
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request(
            "POST",
            "/ops/qc/multiqc",
            {"input": f"~/{work.name}", "outdir": str(other)},
        )
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


@pytest.mark.parametrize(
    ("op", "into_work"),
    [
        (
            "analyze/comprehensive",
            lambda work, other: {
                "vcf_inputs": [str(work / "x.vcf")],
                "outdir": str(other),
            },
        ),
        (
            "analyze/batch-gen",
            lambda work, other: {
                "directory": str(other),
                "output": str(work / "b.csv"),
            },
        ),
    ],
    ids=["vcf-prepared-beside", "batch-file"],
)
def test_an_analyze_job_writing_into_a_busy_folder_waits(
    tmp_path, work, fake_data, slow_coverage, op, into_work
):
    # PR #463 review: comprehensive compresses and indexes a plain VCF beside
    # itself, and batch-gen writes its batch file, yet only outdir was locked.
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request("POST", f"/ops/{op}", into_work(work, other))
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


def test_a_repair_writing_its_output_into_a_busy_folder_waits(
    tmp_path, work, fake_data, slow_coverage
):
    # #478: a repair's --output may name a file outside its outdir.
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request(
            "POST",
            "/ops/repair/ftdna-vcf",
            {
                "input": str(FAKE_DIR / "fake.vcf.gz"),
                "output": str(work / "r.vcf"),
                "outdir": str(other),
            },
        )
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


def test_hla_typing_building_its_library_index_waits_for_a_busy_library(
    tmp_path, work, fake_data, slow_coverage
):
    # #478: `typing hla` without --hla-index builds its T1K index in the
    # reference library, outside its outdir, so that folder is locked too.
    bam, ref = fake_data
    library = work / "library"
    index = library / "ref" / "t1k_hla"
    index.mkdir(parents=True)
    (library / "genomes").mkdir()
    shutil.copy(ref, library / "genomes" / "ref.fa")
    other = tmp_path / "other"
    other.mkdir()
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(index)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request(
            "POST",
            "/ops/typing/hla",
            {
                "input": str(bam),
                "ref": str(library / "genomes" / "ref.fa"),
                "outdir": str(other),
            },
        )
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


def test_a_methylation_run_waits_while_its_reference_folder_is_busy(
    tmp_path, work, fake_data, slow_coverage
):
    # #493 review: an unindexed reference is indexed beside itself.
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(ref, work / "ref.fa")
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        request = {"input": str(bam), "ref": str(work / "ref.fa")}
        status, second = client.request(
            "POST", "/ops/methylation/pileup", {**request, "outdir": str(other)}
        )
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


def test_a_vep_run_on_a_vcf_in_a_busy_folder_waits(
    tmp_path, work, fake_data, slow_coverage
):
    # #478: VEP indexes an unindexed VCF beside itself, outside its outdir.
    bam, ref = fake_data
    other = tmp_path / "other"
    other.mkdir()
    vcf = work / "s.vcf.gz"
    shutil.copy(FAKE_DIR / "fake.vcf.gz", vcf)
    with _serve(tmp_path, [work, other, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request(
            "POST", "/ops/vep/run", {"input": str(vcf), "outdir": str(other)}
        )
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


def test_a_vep_folder_run_refuses_a_link_out_of_the_allowed_folders(tmp_path, work):
    # Copilot review of #491: the guard checks the folder, not the files
    # VEP finds in it.
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(FAKE_DIR / "fake.vcf.gz", outside / "secret.vcf.gz")
    batch = work / "batch"
    batch.mkdir()
    shutil.copy(FAKE_DIR / "fake.vcf.gz", batch / "a.vcf.gz")
    try:
        (batch / "b.vcf.gz").symlink_to(outside / "secret.vcf.gz")
    except OSError:
        pytest.skip("symlinks are not available")
    request = {"input": str(batch), "outdir": str(work)}
    with _serve(tmp_path, [work]) as client:
        status, job = client.request("POST", "/ops/vep/run?wait=120", request)
    assert (status, job["status"], job["exit_code"]) == (200, "failed", 1), job
    assert "b.vcf.gz leads outside" in job["error"]["message"]
    assert not list(work.glob("*_vep*"))


def test_a_server_request_cannot_pass_raw_vep_arguments():
    # #478: --vep-args can name files VEP reads or writes (--custom, -o),
    # which the path guard can't see.
    with pytest.raises(RequestError, match="can't be set by a server request"):
        params_from_json(find("vep/run"), {"input": "a.vcf.gz", "vep_args": "-o x"})


def test_home_relative_paths_reach_the_job_expanded(tmp_path, work, monkeypatch):
    # PR #463 review: the guard approved ~/x as the home folder's; the job
    # must write there too, not in a `~` folder in its working folder.
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(work.parent))
    shutil.copy(FAKE_DIR / "fake.bam", work / "s1.bam")
    request = {"directory": f"~/{work.name}", "output": f"~/{work.name}/b.csv"}
    with _serve(tmp_path, [work]) as client:
        status, job = client.request("POST", "/ops/analyze/batch-gen?wait=120", request)
    assert (status, job["status"]) == (200, "succeeded"), job
    assert "s1" in (work / "b.csv").read_text()


def test_a_trio_saving_beside_its_proband_waits_for_that_folder(
    tmp_path, work, fake_data, slow_coverage
):
    # PR #452 review: the scheduler ignored `proband`, so a trio saving beside
    # its child ran alongside a job writing the same folder.
    bam, ref = fake_data
    with _serve(tmp_path, [work, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        family = {r: str(work / f"{r}.vcf.gz") for r in ("proband", "mother", "father")}
        status, trio = client.request("POST", "/ops/vcf/trio", family)
        assert status == 202, trio
        assert (first["status"], trio["status"]) == ("running", "queued")


def test_a_ref_job_saving_beside_its_fasta_waits_for_that_folder(
    tmp_path, work, fake_data, slow_coverage
):
    # PR #458 review: ref count-ns saves beside its FASTA, yet the server
    # wanted an outdir and didn't serialise the FASTA's folder.
    bam, ref = fake_data
    fasta = work / "ref.fa"
    shutil.copy(ref, fasta)
    with _serve(tmp_path, [work, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, count = client.request("POST", "/ops/ref/count-ns", {"ref": str(fasta)})
        assert status == 202, count
        assert (first["status"], count["status"]) == ("running", "queued")


def test_a_phase_job_saving_beside_its_reads_waits_for_that_folder(
    tmp_path, work, fake_data, slow_coverage
):
    # PR #464 review: vcf phase writes beside its BAM, but the lock took the
    # VCF's folder, so it ran alongside a job writing the BAM's folder.
    bam, ref = fake_data
    reads = work / "reads.bam"
    shutil.copy(bam, reads)
    calls_dir = tmp_path / "calls"
    calls_dir.mkdir()
    calls = calls_dir / "calls.vcf.gz"
    calls.write_bytes(b"")
    with _serve(tmp_path, [work, calls_dir, FAKE_DIR], max_jobs=2) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(work)}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, phase = client.request(
            "POST",
            "/ops/vcf/phase",
            {"input": str(reads), "vcf_input": str(calls), "ref": str(ref)},
        )
        assert status == 202, phase
        assert (first["status"], phase["status"]) == ("running", "queued")


@pytest.mark.parametrize(
    ("op", "params"),
    [
        ("ref/gene-map", lambda library: {"ref": str(library), "delete": True}),
        ("ref/verify", lambda library: {"ref": str(library)}),
    ],
    ids=["installer", "library-folder-fasta"],
)
def test_a_library_job_waits_for_a_job_writing_its_genomes(
    tmp_path, work, fake_data, slow_coverage, op, params
):
    # PR #458 review: installers locked the default output folder, not their
    # library, and `ref verify` of a library folder writes .fai/.gzi into its
    # genomes/, so they ran alongside jobs writing those folders.
    bam, ref = fake_data
    library = work / "library"
    (library / "genomes").mkdir(parents=True)
    shutil.copy(ref, library / "genomes" / "ref.fa")
    # The installer deletes, which downloads nothing.
    with _serve(
        tmp_path, [work, FAKE_DIR], max_jobs=2, allow_destructive=True
    ) as client:
        busy = {"input": str(bam), "ref": str(ref), "outdir": str(library / "genomes")}
        first = client.request("POST", "/ops/info/calculate-coverage", busy)[1]
        status, second = client.request("POST", f"/ops/{op}", params(library))
        assert status == 202, second
        assert (first["status"], second["status"]) == ("running", "queued")


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
        # PR #426 review: a region may name a BED file the guard didn't see.
        (
            "extract/custom",
            {"input": "{work}/a.bam", "region": "{outside}/t.bed"},
            403,
            "path_not_allowed",
        ),
        (
            "extract/bam-subset",
            {"input": "{work}/a.bam", "fraction": 0.5, "region": "beds/t.bed"},
            400,
            "usage",
        ),
        # PR #442 review: with no input or outdir it saved into the server's
        # working folder.
        ("qc/fake-data", {}, 400, "usage"),
        # #346: comprehensive saves into the working folder without outdir.
        ("analyze/comprehensive", {"input": "{work}/a.bam"}, 400, "usage"),
        (
            "analyze/comprehensive",
            {"batch": "{work}/b.csv", "outdir": "{work}"},
            400,
            "usage",
        ),
        ("analyze/batch-gen", {"directory": "{work}"}, 400, "usage"),
        # PR #463 review: "" became None, which the path guard skips.
        ("analyze/batch-gen", {"directory": "{work}", "output": ""}, 400, "usage"),
        (
            "pet-align",
            {"r1": "{outside}/r1.fq", "species": "dog", "outdir": "{work}"},
            403,
            "path_not_allowed",
        ),
        # #351 security review: an unknown format name became part of the
        # template and output paths, so "../" read and wrote outside them.
        (
            "microarray",
            {"input": "{work}/a.bam", "outdir": "{work}", "formats": "../../x"},
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


_IN = r"C:\data\a.bam"


@pytest.mark.parametrize(
    ("op", "body"),
    [
        ("bam/merge", {"inputs": [_IN, _IN], "output": "D:evil.bam"}),
        ("bam/unalign", {"input": _IN, "r1": "D:evil.fq", "r2": "b.fq"}),
        ("bam/unalign", {"input": _IN, "r1": "a.fq", "r2": "D:evil.fq"}),
        ("bam/unalign", {"input": _IN, "r1": "a.fq", "r2": "b.fq", "se": "D:e.fq"}),
    ],
    ids=["merge-output", "unalign-r1", "unalign-r2", "unalign-se"],
)
def test_path_guard_refuses_drive_relative_output_names(op, body):
    # Review of PR #442: on Windows `D:evil.bam` names a file in drive D's
    # working folder, outside every allowed root, yet passed as a bare name.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    request = params_from_json(find(op), {**body, "outdir": r"C:\data"})
    with pytest.raises(RequestError) as refused:
        guard.check(request)
    assert (refused.value.status, refused.value.code) == (400, "usage")
    assert "absolute path" in str(refused.value)
    # The same names without a drive are bare file names, saved in outdir.
    bare = {
        k: v.removeprefix("D:") if isinstance(v, str) else v for k, v in body.items()
    }
    guard.check(params_from_json(find(op), {**bare, "outdir": r"C:\data"}))


_ALIGN_REQUESTS = {
    "align": {"r1": r"C:\data\r1.fq", "ref": r"C:\data\ref.fa"},
    "realign": {"input": r"C:\data\a.bam", "ref": r"C:\data\ref.fa"},
}


@pytest.mark.parametrize(
    ("op", "field"),
    [
        *(("align", f) for f in ("r1", "r2", "input", "ref", "outdir")),
        *(("realign", f) for f in ("input", "r1", "r2", "input_ref", "ref", "outdir")),
    ],
)
@pytest.mark.parametrize(
    "value", ["D:evil.fq", r"D:\evil.fq", r"\\host\share\evil.fq", "evil.fq"]
)
def test_path_guard_covers_every_alignment_path(op, field, value):
    # PR #445: align and realign became server jobs; each file they read or
    # write must be inside an allowed folder, drive-relative names included.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {**_ALIGN_REQUESTS[op], "outdir": r"C:\data"}
    guard.check(params_from_json(find(op), allowed))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


_VCF_FILE_OPS = (
    "annotate", "filter", "table", "to-fasta", "trio", "clinvar", "revel",
    "phylop", "cadd", "gerp", "gnomad", "spliceai", "alphamissense", "pharmgkb",
    "pharmcat", "chain-annotate",
)  # fmt: skip
_VCF_PATHS = [
    (f"vcf/{name}", item)
    for name in _VCF_FILE_OPS
    for item in find(f"vcf/{name}").fields()
    if item.spec.path and not item.spec.local_only
]


@pytest.mark.parametrize(
    ("op", "field"),
    [(op, item.name) for op, item in _VCF_PATHS],
    ids=[f"{op}-{item.name}" for op, item in _VCF_PATHS],
)
@pytest.mark.parametrize(
    "value", ["D:evil.vcf", r"D:\evil.vcf", r"\\host\share\evil.vcf", "evil.vcf"]
)
def test_path_guard_covers_every_vcf_path(op, field, value):
    # #346: the VCF-file commands became server operations; each file they
    # read or write must be inside an allowed folder, drive-relative included.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"vcf_input": r"C:\data\in.vcf.gz", "outdir": r"C:\data"}
    guard.check(params_from_json(find(op), allowed))
    item = next(f for f in find(op).fields() if f.name == field)
    bad = [value] if typing.get_origin(item.base_type) is tuple else value
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: bad}))
    assert refused.value.code in ("usage", "path_not_allowed")


_REF_PATHS = [
    (name, item)
    for name, op in operations().items()
    if name.startswith("ref/")
    for item in op.fields()
    if item.spec.path and not item.spec.local_only
]


_PET_ANALYZE_REQUESTS = {
    "pet-align": {"r1": r"C:\data\r1.fq", "species": "dog"},
    "analyze/comprehensive": {"input": _IN},
    "analyze/batch-gen": {
        "directory": r"C:\data",
        "output": r"C:\data\batch.csv",
    },
}
_PET_ANALYZE_PATHS = [
    (op, item)
    for op in _PET_ANALYZE_REQUESTS
    for item in find(op).fields()
    if item.spec.path and not item.spec.local_only
]


def test_server_offers_no_ref_command_that_fetches_a_url_or_saves_config():
    # #346: ref download fetches any URL and ref bootstrap saves config.toml.
    ref_ops = {name for name in operations() if name.startswith("ref/")}
    assert {"ref/index", "ref/status", "ref/clinvar"} <= ref_ops
    assert not ref_ops & {"ref/download", "ref/bootstrap"}


@pytest.mark.parametrize(
    ("op", "field"),
    [(op, item.name) for op, item in _REF_PATHS],
    ids=[f"{op}-{item.name}" for op, item in _REF_PATHS],
)
@pytest.mark.parametrize(
    "value", ["D:evil.fa", r"D:\evil.fa", r"\\host\share\evil.fa", "evil.fa"]
)
def test_path_guard_covers_every_ref_path(op, field, value):
    # #346: the ref commands became server operations; each file or library
    # they read or write must be inside an allowed folder.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    paths = [item.name for name, item in _REF_PATHS if name == op]
    allowed = {name: rf"C:\data\{name}" for name in paths}
    if op == "ref/library":
        allowed["install"] = "hg38"
    guard.check(params_from_json(find(op), allowed))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


@pytest.mark.parametrize(
    "op", ["ref/gene-map", "ref/clinvar", "ref/status", "ref/library-list"]
)
def test_path_guard_checks_the_library_above_a_genomes_fasta(tmp_path, op):
    # PR #458 review: a FASTA in <library>/genomes names that library, which
    # these commands write into or read, so the library must be allowed too.
    genomes = tmp_path / "library" / "genomes"
    genomes.mkdir(parents=True)
    fasta = genomes / "hg38.fa"
    fasta.write_text(">chr1\nA\n")
    params = params_from_json(find(op), {"ref": str(fasta)})
    with pytest.raises(RequestError) as refused:
        PathGuard([genomes]).check(params)
    assert refused.value.code == "path_not_allowed"
    # status also reads the genome library beside it.
    if op == "ref/status":
        with pytest.raises(RequestError, match="outside the folders"):
            PathGuard([tmp_path / "library"]).check(params)
    PathGuard([tmp_path / "library", tmp_path / "genomes"]).check(params)


@pytest.mark.parametrize("op", ["ref/clinvar", "ref/status", "ref/library-list"])
def test_path_guard_checks_the_library_of_a_home_relative_fasta(
    tmp_path, monkeypatch, op
):
    # PR #458 review: ~/lib/genomes/x.fa wasn't expanded before the library
    # check, so a server allowing only ~/lib/genomes wrote into ~/lib.
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    genomes = tmp_path / "lib" / "genomes"
    genomes.mkdir(parents=True)
    (genomes / "x.fa").write_text(">chr1\nA\n")
    with _serve(tmp_path, [genomes]) as client:
        status, body = client.request(
            "POST", f"/ops/{op}", {"ref": "~/lib/genomes/x.fa"}
        )
    assert (status, body["error"]["code"]) == (403, "path_not_allowed")
    # A Python caller's Params are checked the same way.
    with pytest.raises(RequestError, match="outside the folders"):
        PathGuard([genomes]).check(find(op).params(ref="~/lib/genomes/x.fa"))


@pytest.mark.parametrize(
    ("op", "params"),
    [
        ("bam/merge", {"inputs": ["{home}/a.bam"] * 2, "output": "~/m.bam"}),
        ("bam/unalign", {"input": "{home}/a.bam", "r1": "~/r1.fq", "r2": "r2.fq"}),
        ("extract/custom", {"input": "{home}/a.bam", "region": "~/t.bed"}),
        ("vcf/deepvariant", {"input": "{home}/a.bam", "region": "chr1 ~/t.bed"}),
    ],
)
def test_path_guard_refuses_home_relative_file_names_in_text_fields(
    tmp_path, monkeypatch, op, params
):
    # PR #458 re-review: the guard approved ~/m.bam as the home folder's, but
    # the command used it literally: a `~` folder in the working folder.
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    request = {
        k: [v.format(home=tmp_path) for v in value]
        if isinstance(value, list)
        else value.format(home=tmp_path)
        for k, value in params.items()
    }
    with pytest.raises(RequestError, match="not ~") as refused:
        PathGuard([tmp_path]).check(params_from_json(find(op), request))
    assert refused.value.code == "usage"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reference_library", "outside"),
        ("reference_fasta", "outside/genomes/hg38.fa"),
    ],
)
def test_a_library_list_request_checks_the_library_it_falls_back_to(
    tmp_path, work, monkeypatch, key, value
):
    # #478: with no ref, the configured FASTA's library (else the configured
    # library) is the one read, so it must be an allowed folder too.
    from wgsextract_cli.core.config import settings

    fasta = tmp_path / "outside" / "genomes" / "hg38.fa"
    fasta.parent.mkdir(parents=True)
    fasta.write_text(">chr1\nA\n")
    # The FASTA's library comes first, even when reference_library is allowed.
    monkeypatch.setitem(settings, "reference_library", str(work))
    monkeypatch.setitem(settings, key, str(tmp_path / value))
    with _serve(tmp_path, [work]) as client:
        status, body = client.request("POST", "/ops/ref/library-list", {})
        assert (status, body["error"]["code"]) == (403, "path_not_allowed"), body
        status, body = client.request(
            "POST", "/ops/ref/library-list", {"ref": str(work)}
        )
        assert status == 202, body


@pytest.mark.parametrize("op", ["ref/clinvar", "ref/status", "ref/library-list"])
def test_path_guard_checks_the_library_of_a_fasta_named_through_dotdot(tmp_path, op):
    # PR #499 review: genomes/absent/../x.fa isn't a file as written, so the
    # guard checked it as a folder inside genomes, while the command
    # collapsed the `..` and read the library above.
    genomes = tmp_path / "lib" / "genomes"
    genomes.mkdir(parents=True)
    (genomes / "x.fa").write_text(">chr1\nA\n")
    params = params_from_json(find(op), {"ref": f"{genomes}/absent/../x.fa"})
    with pytest.raises(RequestError, match="outside the folders"):
        PathGuard([genomes]).check(params)


def test_a_library_list_request_checks_a_configured_fasta_link(
    tmp_path, work, monkeypatch
):
    # PR #499 review: with no ref, the configured FASTA is read too; a link
    # in an allowed library to a FASTA elsewhere must be refused.
    from wgsextract_cli.core.config import settings

    outside = tmp_path / "outside.fa"
    outside.write_text(">chr1\nA\n")
    (work / "genomes").mkdir()
    link = work / "genomes" / "hg38.fa"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links need privileges here")
    monkeypatch.setitem(settings, "reference_fasta", str(link))
    with pytest.raises(RequestError, match="outside the folders"):
        PathGuard([work]).check(find("ref/library-list").params())
    PathGuard([work, outside]).check(find("ref/library-list").params())


def test_ref_library_list_is_a_query_with_the_listing_as_details(client, work):
    (work / "genomes").mkdir()
    (work / "genomes" / "hs38.fa.gz").write_bytes(b"x")
    _, ops = client.request("GET", "/ops")
    assert {op["op"]: op["kind"] for op in ops["ops"]}["ref/library-list"] == "query"

    status, job = client.request("POST", "/ops/ref/library-list", {"ref": str(work)})
    assert status == 202, job
    client.events(job["id"])
    _, job = client.request("GET", f"/jobs/{job['id']}")
    assert job["status"] == "succeeded", job
    details = job["result"]["details"]
    assert details["library"] == str(work)
    assert {g["code"]: g["status"] for g in details["genomes"]}["hs38"] == ("unindexed")


def test_ref_library_requests_must_name_a_genome():
    # PR #458 review: a request without `install` was accepted as a job.
    with pytest.raises(RequestError, match="ref library --list") as refused:
        params_from_json(find("ref/library"), {})
    assert refused.value.code == "usage"


@pytest.mark.parametrize(
    ("op", "field"),
    [(op, item.name) for op, item in _PET_ANALYZE_PATHS],
    ids=[f"{op}-{item.name}" for op, item in _PET_ANALYZE_PATHS],
)
@pytest.mark.parametrize(
    "value", ["D:evil.fq", r"D:\evil.fq", r"\\host\share\evil.fq", "evil.fq"]
)
def test_path_guard_covers_every_pet_and_analyze_path(op, field, value):
    # #346: pet-align and analyze became server operations; each file they
    # read or write must be inside an allowed folder, drive-relative included.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {**_PET_ANALYZE_REQUESTS[op], "outdir": r"C:\data"}
    if "outdir" not in {f.name for f in find(op).fields()}:
        del allowed["outdir"]
    guard.check(params_from_json(find(op), allowed))
    item = next(f for f in find(op).fields() if f.name == field)
    bad = [value] if typing.get_origin(item.base_type) is tuple else value
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: bad}))
    assert refused.value.code in ("usage", "path_not_allowed")


def test_pet_and_analyze_paths_cover_every_file():
    assert {(op, item.name) for op, item in _PET_ANALYZE_PATHS} == {
        ("pet-align", "r1"), ("pet-align", "r2"), ("pet-align", "ref"),
        ("pet-align", "outdir"), ("analyze/comprehensive", "vcf_inputs"),
        ("analyze/comprehensive", "input"), ("analyze/comprehensive", "ref"),
        ("analyze/comprehensive", "outdir"), ("analyze/batch-gen", "directory"),
        ("analyze/batch-gen", "output"),
    }  # fmt: skip


def test_vcf_paths_cover_every_resource():
    # Every annotation data file, the trio's members and outside calls are
    # guarded; PharmCAT's pipeline folder runs code, so requests can't set it.
    guarded = {item.name for _, item in _VCF_PATHS}
    assert {"clinvar_file", "cadd_indel_file", "am_file", "mother", "father",
            "proband", "mask", "pharmcat_json", "outside_call",
            "ann_vcf"} <= guarded  # fmt: skip
    # PR #452 review: <ref>/pharmcat is the pipeline's default folder.
    for field in ("pharmcat_dir", "ref"):
        with pytest.raises(RequestError, match="can't be set by a server request"):
            params_from_json(find("vcf/pharmcat"), {field: r"C:\data\pc"})


def test_path_guard_refuses_file_operands_in_expressions():
    # PR #452 review: bcftools reads `@FILE` in an expression, past the guard.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"vcf_input": r"C:\data\in.vcf.gz", "expr": "QUAL>30"}
    guard.check(params_from_json(find("vcf/filter"), allowed))
    for expr in ("ID=@/outside/ids.txt", r"ID!=@\\host\share\ids"):
        with pytest.raises(RequestError, match="@FILE") as refused:
            guard.check(params_from_json(find("vcf/filter"), {**allowed, "expr": expr}))
        assert refused.value.code == "path_not_allowed"


_CALLER_OPS = ("cnv", "sv", "freebayes", "gatk", "deepvariant", "clair3", "phase")
_CALLER_PATHS = [
    (f"vcf/{name}", item)
    for name in _CALLER_OPS
    for item in find(f"vcf/{name}").fields()
    if item.spec.path or item.spec.may_name_file
]


@pytest.mark.parametrize(
    ("op", "field"),
    [(op, item.name) for op, item in _CALLER_PATHS],
    ids=[f"{op}-{item.name}" for op, item in _CALLER_PATHS],
)
@pytest.mark.parametrize(
    "value", ["D:evil.bed", r"D:\evil.bed", r"\\host\share\evil.bed", r"sub\evil.bed"]
)
def test_path_guard_covers_every_caller_path(op, field, value):
    # #346: the BAM/CRAM variant callers became server operations; each file
    # they read or write (a DeepVariant region BED, a Clair3 model folder
    # included) must be inside an allowed folder, drive-relative included.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"input": r"C:\data\a.bam", "outdir": r"C:\data"}
    guard.check(params_from_json(find(op), allowed))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


_REPAIR_PATHS = [
    (op, item)
    for op in ("repair/ftdna-bam", "repair/ftdna-vcf")
    for item in find(op).fields()
    if item.spec.path or item.spec.may_name_file
]


@pytest.mark.parametrize(
    ("op", "field"),
    [(op, item.name) for op, item in _REPAIR_PATHS],
    ids=[f"{op}-{item.name}" for op, item in _REPAIR_PATHS],
)
@pytest.mark.parametrize(
    "value", ["D:evil.bam", r"D:\evil.bam", r"\\host\share\evil.bam", r"sub\evil.bam"]
)
def test_path_guard_covers_every_repair_path(op, field, value):
    # #478: the FTDNA repairs became server operations; the file each reads
    # or writes (an --output path included) must be inside an allowed folder.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"input": r"C:\data\a.bam", "outdir": r"C:\data"}
    guard.check(params_from_json(find(op), allowed))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


def test_repair_paths_cover_every_file():
    assert {(op, item.name) for op, item in _REPAIR_PATHS} == {
        ("repair/ftdna-bam", "input"), ("repair/ftdna-bam", "output"),
        ("repair/ftdna-bam", "ref"), ("repair/ftdna-bam", "outdir"),
        ("repair/ftdna-vcf", "input"), ("repair/ftdna-vcf", "output"),
        ("repair/ftdna-vcf", "outdir"),
    }  # fmt: skip


_TYPING_PATHS = [
    (op, item)
    for op in ("typing/cyp2d6", "typing/hla", "typing/str")
    for item in find(op).fields()
    if item.spec.path or item.spec.may_name_file
]


_VEP_PATHS = [
    item
    for item in find("vep/run").fields()
    if item.spec.path or item.spec.may_name_file
]


@pytest.mark.parametrize(
    ("op", "field"),
    [(op, item.name) for op, item in _TYPING_PATHS],
    ids=[f"{op}-{item.name}" for op, item in _TYPING_PATHS],
)
@pytest.mark.parametrize(
    "value",
    ["D:evil.fa", r"D:\evil.fa", r"\\host\share\evil.fa", r"sub\evil.fa", "~/evil.fa"],
)
def test_path_guard_covers_every_typing_path(op, field, value):
    # #478: the typing commands became server operations; each file they read
    # (an HLA index or STR catalog included) must be inside an allowed folder.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"input": r"C:\data\a.bam", "outdir": r"C:\data"}
    guard.check(params_from_json(find(op), allowed))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


def test_typing_paths_cover_every_file():
    assert {(op, item.name) for op, item in _TYPING_PATHS} == {
        *((f"typing/{name}", f) for name in ("cyp2d6", "hla", "str")
          for f in ("input", "ref", "outdir")),
        ("typing/hla", "hla_index"), ("typing/str", "catalog"),
    }  # fmt: skip


def test_path_guard_checks_the_library_hla_typing_builds_its_index_in(tmp_path):
    # #478: without --hla-index, `typing hla` builds its T1K index in the
    # reference library above a <library>/genomes FASTA, so that library must
    # be allowed; an explicit index needs no library.
    genomes = tmp_path / "library" / "genomes"
    genomes.mkdir(parents=True)
    fasta = genomes / "hg38.fa"
    fasta.write_text(">chr1\nA\n")
    request = {"input": str(genomes / "a.bam"), "ref": str(fasta)}
    with pytest.raises(RequestError) as refused:
        PathGuard([genomes]).check(params_from_json(find("typing/hla"), request))
    assert refused.value.code == "path_not_allowed"
    PathGuard([genomes]).check(
        params_from_json(
            find("typing/hla"), {**request, "hla_index": str(genomes / "i.fa")}
        )
    )
    PathGuard([tmp_path / "library"]).check(
        params_from_json(find("typing/hla"), request)
    )


def test_path_guard_checks_where_hla_typing_would_build_its_index(
    tmp_path, monkeypatch
):
    # PR #492 review: the guard checked the library, not the index folder
    # hla_index() picks, so a symlinked <library>/ref sent the build outside;
    # and a ref filled from config was not checked at all.
    library = tmp_path / "library"
    (library / "genomes").mkdir(parents=True)
    fasta = library / "genomes" / "hg38.fa"
    fasta.write_text(">chr1\nA\n")
    request = {"input": str(library / "a.bam"), "ref": str(fasta)}
    PathGuard([library]).check(params_from_json(find("typing/hla"), request))

    monkeypatch.setitem(settings, "reference_fasta", str(fasta))
    without_ref = {"input": str(library / "a.bam")}
    with pytest.raises(RequestError) as refused:
        PathGuard([library / "genomes"]).check(
            params_from_json(find("typing/hla"), without_ref)
        )
    assert refused.value.code == "path_not_allowed"

    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (library / "ref").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("can't create symlinks here")
    with pytest.raises(RequestError) as refused:
        PathGuard([library]).check(params_from_json(find("typing/hla"), request))
    assert refused.value.code == "path_not_allowed"


@pytest.mark.parametrize("field", [item.name for item in _VEP_PATHS])
@pytest.mark.parametrize(
    "value", ["D:evil.vcf", r"D:\evil.vcf", r"\\host\share\evil.vcf", r"sub\evil.vcf"]
)
def test_path_guard_covers_every_vep_path(field, value):
    # #478: `vep run` became a server operation; every file or folder it
    # reads or writes must be inside an allowed folder.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"input": r"C:\data\a.vcf.gz", "outdir": r"C:\data"}
    guard.check(params_from_json(find("vep/run"), allowed))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find("vep/run"), {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


def test_vep_paths_cover_every_file():
    assert {item.name for item in _VEP_PATHS} == {
        "input", "ref", "outdir", "vep_cache", "ploidy_file"
    }  # fmt: skip


def test_caller_paths_cover_every_file():
    guarded = {(op, item.name) for op, item in _CALLER_PATHS}
    assert {("vcf/deepvariant", "checkpoint"), ("vcf/deepvariant", "region"),
            ("vcf/clair3", "model"), ("vcf/sv", "tandem_repeats"),
            *((f"vcf/{name}", f) for name in _CALLER_OPS
              for f in ("input", "ref", "outdir"))} <= guarded  # fmt: skip
    # Region text and a bundled model's name aren't files, so they pass.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"input": r"C:\data\a.bam", "outdir": r"C:\data"}
    guard.check(
        params_from_json(find("vcf/deepvariant"), {**allowed, "region": "chr20:1-9"})
    )
    guard.check(
        params_from_json(
            find("vcf/clair3"), {**allowed, "model": "r1041_e82_400bps_sup_v500"}
        )
    )


_METHYLATION_PATHS = [
    item
    for item in find("methylation/pileup").fields()
    if item.spec.path or item.spec.may_name_file
]


@pytest.mark.parametrize("field", [item.name for item in _METHYLATION_PATHS])
@pytest.mark.parametrize(
    "value", ["D:evil.bam", r"D:\evil.bam", r"\\host\share\evil.bam", r"sub\evil.bam"]
)
def test_path_guard_covers_every_methylation_path(field, value):
    # #478: `methylation pileup` became a server operation; every file or
    # folder it reads or writes must be inside an allowed folder.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    allowed = {"input": r"C:\data\a.bam", "outdir": r"C:\data"}
    op = find("methylation/pileup")
    guard.check(params_from_json(op, {**allowed, "region": "chr20:1-9,chr21"}))
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(op, {**allowed, field: value}))
    assert refused.value.code in ("usage", "path_not_allowed")


def test_methylation_paths_cover_every_file():
    assert {item.name for item in _METHYLATION_PATHS} == {"input", "ref", "outdir"}


# Each backend's region list: DeepVariant splits on whitespace, Parabricks
# (deepvariant/gatk --gpu) and GATK on commas.
_REGION_LISTS = [
    ("vcf/deepvariant", {}, " "),
    ("vcf/deepvariant", {"gpu": True}, ","),
    ("vcf/gatk", {}, ","),
]


@pytest.mark.parametrize(("op", "extra", "sep"), _REGION_LISTS)
def test_path_guard_checks_every_file_in_a_region_list(
    op, extra, sep, tmp_path, monkeypatch
):
    # Review of PR #454: DeepVariant reads every whitespace-separated BED file
    # in its region, and Parabricks every comma-separated one, but the guard
    # checked the region as one path: an allowed BED hid an outside file, and
    # a bare name after a region read the server's working folder.
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    for folder in (allowed, outside):
        folder.mkdir()
        (folder / "a.bed").write_text("chr1\t0\t9\n")
    monkeypatch.chdir(outside)
    guard = PathGuard([allowed])
    body = {"input": str(allowed / "a.bam"), "outdir": str(allowed), **extra}
    inside = str(allowed / "a.bed")
    for region in (f"{inside}{sep}chr1", f"chr1:1-9{sep}chr2"):
        guard.check(params_from_json(find(op), {**body, "region": region}))
    for region, code in (
        (f"{inside}{sep}{outside / 'a.bed'}", "path_not_allowed"),
        (f"chr1{sep}a.bed", "usage"),
    ):
        with pytest.raises(RequestError) as refused:
            guard.check(params_from_json(find(op), {**body, "region": region}))
        assert refused.value.code == code, region


@pytest.mark.parametrize(
    ("extra", "name"), [({}, "panel,2026"), ({"gpu": True}, "panel files")]
)
def test_path_guard_splits_a_region_list_as_its_backend_does(extra, name, tmp_path):
    # Review of PR #454: a folder name with the other backend's delimiter is
    # one allowed path, not a refused fragment.
    bed = tmp_path / name / "targets.bed"
    bed.parent.mkdir()
    bed.write_text("chr1\t0\t9\n")
    body = {"input": str(tmp_path / "a.bam"), "outdir": str(tmp_path), **extra}
    PathGuard([tmp_path]).check(
        params_from_json(find("vcf/deepvariant"), {**body, "region": str(bed)})
    )


@pytest.mark.parametrize(
    ("extra", "region"),
    [
        ({}, r"C:\data\a.bed D:\evil.bed"),
        ({"gpu": True}, r"C:\data\a.bed,D:evil.bed"),
        ({}, "chr1 D:evil.bed"),
        ({}, "D:evil.bed"),
        ({}, r"chr1 \\host\share\evil.bed"),
        ({}, r"C:\data\a.bed sub\evil.bed"),
    ],
)
def test_path_guard_checks_every_windows_file_in_a_region_list(extra, region):
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    body = {"input": r"C:\data\a.bam", "outdir": r"C:\data", **extra}
    with pytest.raises(RequestError) as refused:
        guard.check(
            params_from_json(find("vcf/deepvariant"), {**body, "region": region})
        )
    assert refused.value.code in ("usage", "path_not_allowed")


@pytest.mark.parametrize(
    "region", [r"C:\data\a.bed chr1", "X:1-100", "1:1-100 Y:5,000-", "X"]
)
def test_path_guard_reads_a_windows_interval_as_a_region(region):
    # Review of PR #454: on Windows "X:1-100" has a drive, so it was refused
    # as a drive-relative path.
    guard = PathGuard([r"C:\data"], pathmod=_RecordingFlavour())
    body = {"input": r"C:\data\a.bam", "outdir": r"C:\data"}
    guard.check(params_from_json(find("vcf/deepvariant"), {**body, "region": region}))


@pytest.mark.parametrize("depth", [-1, float("nan")])
def test_a_negative_or_nan_depth_is_refused(depth):
    # Review of PR #454: --qc-min-depth refused them, the API didn't.
    with pytest.raises(UsageError, match="at least 0"):
        find("vcf/deepvariant").params(input=Path("a.bam"), qc_min_depth=depth)
    if depth == -1:
        with pytest.raises(RequestError):
            params_from_json(
                find("vcf/deepvariant"), {"input": "/a.bam", "qc_min_depth": depth}
            )
    schema = json_schema(find("vcf/deepvariant").params)
    assert schema["properties"]["qc_min_depth"]["minimum"] == 0


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


_DATA = r"C:\data"
_FILE = _DATA + r"\in.vcf.gz"
# Requests each operation accepts with every path inside _DATA, beyond its
# required fields; a field's companions are what it needs beside it.
_BASE_REQUESTS: dict[str, dict[str, Any]] = {
    "align": {"r1": _FILE},
    "pet-align": {"r1": _FILE},
    "qc/fastp": {"r1": _FILE},
    "analyze/batch-gen": {"output": _DATA + r"\batch.csv"},
    "bam/to-ubam": {"input": _FILE},
    "extract/custom": {"region": "chrM"},
    "info/chip": {"input": _FILE},
    "panel/bed": {"genes": ["BRCA1"]},
    "qc/vcf": {"input": _FILE},
    "ref/library": {"install": "hs38"},
    "ref/library-list": {"ref": _DATA},
    "report": {"input": _FILE},
    "sample/show": {"input": _FILE},
    "sample/set": {"input": _FILE, "sex": "female"},
    "sample/unset": {"input": _FILE, "sex": True},
}
_COMPANIONS: dict[tuple[str, str], dict[str, Any]] = {
    ("basecall", "regions_bed"): {"alignment": _FILE, "regions_bed": _FILE},
    ("basecall", "alignment"): {"alignment": _FILE, "regions_bed": _FILE},
    ("bam/to-ubam", "r1"): {"input": None, "r1": _FILE},
    ("bam/to-ubam", "r2"): {"input": None, "r1": _FILE},
}


def _path_value(item: Any, path: str) -> Any:
    return [path] if typing.get_origin(item.base_type) in (tuple, list) else path


def _filler(item: Any) -> Any:
    if item.spec.path:
        return _path_value(item, _DATA if item.spec.path == "directory" else _FILE)
    if item.spec.choices:
        return item.spec.choices[0]
    return {int: 1, float: 1.0, bool: True}.get(item.base_type, "x")


def _base_request(name: str) -> dict[str, Any]:
    op = find(name)
    body = {f.name: _filler(f) for f in op.fields() if f.required}
    if any(f.name == "outdir" for f in op.fields()):
        body["outdir"] = _DATA
    return {**body, **_BASE_REQUESTS.get(name, {})}


_EVERY_PATH = [
    (name, item.name)
    for name, op in operations().items()
    for item in op.fields()
    if item.spec.path and not item.spec.local_only
]


@pytest.mark.parametrize(("op", "field"), _EVERY_PATH, ids=map("-".join, _EVERY_PATH))
def test_every_path_of_every_operation_must_be_inside_an_allowed_folder(op, field):
    # #351 phase 2: the typed API brought 88 operations to the server with no
    # server code each. Whatever an operation is, a file it names outside the
    # allowed folders is refused before it runs.
    guard = PathGuard([_DATA], allow_destructive=True, pathmod=_RecordingFlavour())
    body = {**_base_request(op), **_COMPANIONS.get((op, field), {})}
    guard.check(params_from_json(find(op), body))
    item = next(f for f in find(op).fields() if f.name == field)
    outside = {**body, field: _path_value(item, r"E:\elsewhere\x.vcf.gz")}
    with pytest.raises(RequestError) as refused:
        guard.check(params_from_json(find(op), outside))
    assert (refused.value.status, refused.value.code) == (403, "path_not_allowed")


@pytest.mark.parametrize(
    "region", ["chr1,-U/x/leak.bam", "-o/x/leak.bam", "chr1 --output=/x/leak.bam"]
)
def test_a_region_item_that_looks_like_an_option_is_refused(client, work, region):
    # #351 phase 2 security review: vcf phase --haplotag passed each region
    # item to `samtools view`, so `-U/any/file` wrote outside the allowed
    # folders, on the CLI too.
    with pytest.raises(UsageError, match="isn't a region"):
        find("vcf/phase").params(input=work / "a.bam", region=region, haplotag=True)
    body = {"input": str(work / "a.bam"), "region": region, "haplotag": True}
    status, response = client.request("POST", "/ops/vcf/phase", body)
    assert (status, response["error"]["code"]) == (400, "usage")
    assert client.request("GET", "/jobs")[1]["jobs"] == []


@pytest.mark.parametrize(
    ("gpu", "accepted", "refused"),
    [
        (False, "/d/panel,-v2/t.bed", "/d/panel -old/t.bed"),
        (True, "/d/panel -old/t.bed", "/d/panel,-v2/t.bed"),
    ],
)
def test_region_items_are_split_as_the_backend_splits_them(
    work, gpu, accepted, refused
):
    # Review of #486: every region was split on commas and spaces, refusing
    # BED paths DeepVariant (spaces) or Parabricks (commas) reads as one item.
    op = find("vcf/deepvariant")
    assert op.params(input=work / "a.bam", region=accepted, gpu=gpu).region == accepted
    with pytest.raises(UsageError, match="isn't a region"):
        op.params(input=work / "a.bam", region=refused, gpu=gpu)


def _indexed_bam(work: Path, bam: Path) -> Path:
    copy = work / "s.bam"
    shutil.copyfile(bam, copy)
    subprocess.run(["samtools", "index", str(copy)], check=True)
    return copy


def test_deleting_data_needs_allow_destructive(tmp_path, work, fake_data):
    # #351 asks for an explicit opt-in before a request deletes data the
    # server didn't make: an index, a sample's settings, the gene maps.
    bam = _indexed_bam(work, fake_data[0])
    index = Path(f"{bam}.bai")
    with _serve(tmp_path, [work]) as client:
        listed = {o["op"]: o for o in client.request("GET", "/ops")[1]["ops"]}
        assert listed["bam/unindex"]["destructive"] is True
        assert listed["ref/gene-map"]["destructive"] is True
        assert "destructive" not in listed["bam/index"]
        status, response = client.request(
            "POST", "/ops/bam/unindex", {"input": str(bam)}
        )
        assert (status, response["error"]["code"]) == (403, "destructive")
        status, response = client.request(
            "POST", "/ops/ref/gene-map", {"ref": str(work), "delete": True}
        )
        assert (status, response["error"]["code"]) == (403, "destructive")
        assert client.request("GET", "/jobs")[1]["jobs"] == []
    assert index.exists()
    with _serve(tmp_path / "again", [work], allow_destructive=True) as client:
        status, job = client.request(
            "POST", "/ops/bam/unindex?wait=60", {"input": str(bam)}
        )
    assert (status, job["status"]) == (200, "succeeded")
    assert not index.exists() and bam.exists()


def test_mcp_offers_deleting_tools_only_with_allow_destructive(work):
    default = Mcp("--allow", str(work), "--allow-jobs")
    names = {t["name"]: t for t in default.request("tools/list", {})["result"]["tools"]}
    assert default.close() == 0
    assert "bam_unindex" not in names and "sample_unset" not in names
    assert names["ref_gene_map"]["annotations"]["destructiveHint"] is False
    allowed = Mcp("--allow", str(work), "--allow-jobs", "--allow-destructive")
    names = {t["name"]: t for t in allowed.request("tools/list", {})["result"]["tools"]}
    assert names["bam_unindex"]["annotations"]["destructiveHint"] is True
    assert names["ref_gene_map"]["annotations"]["destructiveHint"] is True
    assert names["bam_index"]["annotations"]["destructiveHint"] is False
    assert allowed.close() == 0


def _pharmcat_run(sample: Path, folder: Path) -> None:
    """A `vcf pharmcat` manifest in ``sample`` whose phenotypes are in ``folder``."""
    phenotypes = {
        "geneReports": {
            "CYP2C19": {
                "recommendationDiplotypes": [
                    {
                        "allele1": {"name": "*1"},
                        "allele2": {"name": "*17"},
                        "phenotypes": ["Rapid Metabolizer"],
                    }
                ]
            }
        }
    }
    evidence = folder / "HG002.phenotype.json"
    evidence.write_text(json.dumps(phenotypes))
    manifest = sample / "HG002.phenotype.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "wgsextract.result/1",
                "command": "vcf pharmcat",
                "status": "ok",
                "exit_code": 0,
                "inputs": [{"path": str(sample / "HG002.vcf.gz"), "role": "input"}],
                "outputs": [{"path": str(evidence), "role": "pharmcat_phenotypes"}],
                "manifest": str(manifest),
                "provenance": {"finished": "2025-01-01T00:00:00+00:00"},
            }
        )
    )


@pytest.mark.parametrize("through_link", [False, True])
def test_a_server_report_reads_no_evidence_outside_the_allowed_folders(
    tmp_path, work, through_link
):
    # #351 phase 2 security review: report reads the outputs earlier runs'
    # manifests list, wherever they are, so a manifest in an allowed folder
    # could lead a server job to any file. Review of #486: an allowed folder
    # named through a symlink must still count as inside.
    allowed = work
    if through_link:
        allowed = tmp_path / "link"
        try:
            allowed.symlink_to(work, target_is_directory=True)
        except OSError:
            pytest.skip("can't create symlinks here")
    sample = allowed / "HG002"
    sample.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    def genes(job: dict[str, Any]) -> set[str]:
        assert job["status"] == "succeeded", job
        (document,) = (
            o["path"] for o in job["result"]["outputs"] if o["path"].endswith(".json")
        )
        section = json.loads(Path(document).read_text())["sections"]["pharmacogenomics"]
        return {f["gene"] for f in section["findings"]}

    def over_http(client: Client) -> set[str]:
        body = {"workspace": str(sample)}
        return genes(client.request("POST", "/ops/report?wait=120", body)[1])

    with _serve(tmp_path, [allowed]) as client:
        _pharmcat_run(sample, sample)
        assert over_http(client) == {"CYP2C19"}
        _pharmcat_run(sample, outside)
        assert over_http(client) == set()
    mcp = Mcp("--allow", str(allowed), "--allow-jobs")

    def over_mcp() -> set[str]:
        started = mcp.call("report", workspace=str(sample))["structuredContent"]
        job = mcp.call("job_status", job_id=started["id"], wait_seconds=120)
        return genes(job["structuredContent"])

    assert over_mcp() == set()
    _pharmcat_run(sample, sample)
    assert over_mcp() == {"CYP2C19"}
    assert mcp.close() == 0


def test_a_server_report_never_touches_a_refused_recorded_path(
    tmp_path, work, monkeypatch
):
    # Review of #486: report checked that a recorded output existed before
    # asking the read scope, so a manifest naming a UNC path made a server
    # job connect to that host (sending this user's credentials on Windows).
    from tests.htslib_helpers import run_wgsextract
    from wgsextract_cli.core import read_scope

    sample = work / "HG002"
    sample.mkdir()
    share = "//host/share"
    for command, role, name in (
        ("vcf pharmcat", "pharmcat_phenotypes", "HG002.phenotype.json"),
        ("microarray", "combined_kit", "HG002_CombinedKit.txt"),
    ):
        manifest = sample / f"{name}.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "wgsextract.result/1",
                    "command": command,
                    "status": "ok",
                    "exit_code": 0,
                    "outputs": [{"path": f"{share}/{name}", "role": role}],
                    "manifest": str(manifest),
                    "provenance": {"finished": "2025-01-01T00:00:00+00:00"},
                }
            )
        )
    probed: list[str] = []
    stat = os.stat

    def spy(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path).replace("\\", "/").startswith(share):
            probed.append(os.fspath(path))
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)
    with read_scope.limited_to(PathGuard([work]).allows):
        run_wgsextract("report", "--workspace", str(sample))
    assert probed == []
    report = json.loads((sample / "HG002_personal_report.json").read_text())
    assert report["sections"]["pharmacogenomics"]["findings"] == []
    assert [f["exists"] for f in report["sections"]["microarray"]["files"]] == [False]


_TABLE_HEADER = """##fileformat=VCFv4.2
##contig=<ID=chr1,length=1000>
##contig=<ID=chr2,length=1000>
##INFO=<ID=CLNSIG,Number=.,Type=String,Description="ClinVar significance">
##INFO=<ID=WGSE_CONF,Number=A,Type=String,Description="Confidence">
##INFO=<ID=CSQ,Number=.,Type=String,Description="Consequence annotations from Ensembl VEP. Format: Allele|Consequence|IMPACT|SYMBOL">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1
"""
# (chrom, pos, id, ref, alt, gene, ClinVar, confidence)
_TABLE_RECORDS = [
    ("chr1", 100, "rs1", "A", "G", "GENEA", "Pathogenic", "high"),
    ("chr1", 200, "rs2", "C", "T", "GENEA", "Likely_pathogenic", "moderate"),
    ("chr1", 300, "rs22", "G", "A", "GENEB", "Benign", "low"),
    ("chr1", 400, "rs4", "T", "C", "GENEB", "Pathogenic/Likely_pathogenic", "high"),
    ("chr2", 50, "rs5", "A", "T", "GENEC", None, "high"),
]


@pytest.fixture
def variant_tables(work: Path) -> dict[str, Path]:
    from tests.htslib_helpers import bgzip_index, run_wgsextract

    lines = [
        "\t".join(
            [
                chrom,
                str(pos),
                rsid,
                ref,
                alt,
                "50",
                "PASS",
                ";".join(
                    ([f"CLNSIG={clnsig}"] if clnsig else [])
                    + [
                        f"WGSE_CONF={conf}",
                        f"CSQ={alt}|missense_variant|MODERATE|{gene}",
                    ]
                ),
                "GT",
                "0/1",
            ]
        )  # fmt: skip
        for chrom, pos, rsid, ref, alt, gene, clnsig, conf in _TABLE_RECORDS
    ]
    plain = work / "sample.vcf"
    plain.write_text(_TABLE_HEADER + "\n".join(lines) + "\n")
    vcf = bgzip_index(plain)
    run_wgsextract("vcf", "table", "--vcf-input", vcf, "--outdir", str(work))
    tables = {
        "sqlite": work / "sample_variants.sqlite",
        "tsv": work / "sample_variants.tsv",
    }
    if importlib.util.find_spec("pyarrow"):
        run_wgsextract(
            "vcf", "table", "--vcf-input", vcf, "--outdir", str(work),
            "--no-sqlite", "--format", "parquet",
        )  # fmt: skip
        tables["parquet"] = work / "sample_variants.parquet"
    return tables


@pytest.mark.parametrize("kind", ["sqlite", "tsv", "parquet"])
def test_variant_lookups_filter_and_page_every_table_format(
    tmp_path, work, variant_tables, kind
):
    if kind not in variant_tables:
        pytest.skip("pyarrow not installed")
    table = variant_tables[kind]

    def lookup(**query: Any) -> tuple[int, Any]:
        text = urllib.parse.urlencode({"path": str(table), **query})
        return client.request("GET", f"/variants?{text}")

    def positions(**query: Any) -> list[tuple[str, int]]:
        status, answer = lookup(**query)
        assert status == 200, answer
        return [(row["chrom"], row["pos"]) for row in answer["rows"]]

    with _serve(tmp_path, [work]) as client:
        assert positions(gene="GENEA") == [("chr1", 100), ("chr1", 200)]
        # rs2 is not rs22.
        assert positions(rsid="rs2") == [("chr1", 200)]
        assert positions(region="chr1:150-350") == [("chr1", 200), ("chr1", 300)]
        assert positions(region="chr1:300") == [("chr1", 300)]
        assert positions(region="chr2") == [("chr2", 50)]
        assert positions(clinvar="likely_pathogenic") == [("chr1", 200), ("chr1", 400)]
        assert positions(min_confidence="moderate", gene="GENEB") == [("chr1", 400)]
        status, answer = lookup(gene="GENEB", columns="pos,clinvar_significance")
        assert answer["rows"] == [
            {"pos": 300, "clinvar_significance": "Benign"},
            {"pos": 400, "clinvar_significance": "Pathogenic/Likely_pathogenic"},
        ]

        # Review of #351 PR B: OpenAPI clients repeat array parameters, and
        # a region past SQLite's integers was a 500.
        status, answer = client.request(
            "GET",
            f"/variants?path={urllib.parse.quote(str(table))}&rsid=rs4"
            "&columns=pos&columns=gene",
        )
        assert answer["rows"] == [{"pos": 400, "gene": "GENEB"}]
        assert positions(region=f"chr1:{10**20}-{10**21}") == []

        pages: list[list[int]] = []
        cursor = ""
        while cursor is not None:
            status, answer = lookup(limit=2, **({"cursor": cursor} if cursor else {}))
            pages.append([row["pos"] for row in answer["rows"]])
            cursor = answer["next_cursor"]
        assert pages == [[100, 200], [300, 400], [50]]
        status, answer = lookup(gene="GENEB", limit=1)
        assert [row["pos"] for row in answer["rows"]] == [300]
        status, answer = lookup(gene="GENEB", limit=1, cursor=answer["next_cursor"])
        assert ([row["pos"] for row in answer["rows"]], answer["next_cursor"]) == (
            [400],
            None,
        )

        # Review of #487: an empty start or end, a non-ASCII digit and a
        # cursor past SQLite's integers were 500s.
        for bad in (
            {"limit": 0}, {"rsid": "2"}, {"region": "chr1:9-1"}, {"x": 1},
            {"region": "chr1:,"}, {"region": "chr1:1-,"}, {"cursor": "\u00b2"},
            {"cursor": str(2**63)}, {"cursor": "-1"},
        ):  # fmt: skip
            status, answer = lookup(**bad)
            assert (status, answer["error"]["code"]) == (400, "usage"), bad
        outside = tmp_path / table.name
        shutil.copy(table, outside)
        status, answer = client.request(
            "GET", f"/variants?{urllib.parse.urlencode({'path': str(outside)})}"
        )
        assert (status, answer["error"]["code"]) == (403, "path_not_allowed")


def test_unreadable_variant_tables_are_refused_not_failed(
    tmp_path, work, variant_tables, monkeypatch
):
    # Review of #351 PR B: a truncated download and a missing pyarrow were
    # internal errors (500) instead of saying what's wrong.
    whole = gzip.compress(variant_tables["tsv"].read_bytes() * 200)
    truncated = work / "cut_variants.tsv.gz"
    truncated.write_bytes(whole[: len(whole) // 2])
    parquet = work / "sample_variants.parquet"
    parquet.touch()
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    with _serve(tmp_path, [work]) as client:
        for table in (truncated, parquet):
            text = urllib.parse.urlencode({"path": str(table), "limit": 1000})
            status, answer = client.request("GET", f"/variants?{text}")
            assert (status, answer["error"]["code"]) == (400, "usage"), table
        assert "pyarrow" in answer["error"]["message"]


def test_report_lookup_reads_a_samples_newest_report(tmp_path, work):
    from tests.htslib_helpers import run_wgsextract

    sample = work / "HG002"
    sample.mkdir()
    _pharmcat_run(sample, sample)
    run_wgsextract("report", "--workspace", str(sample))
    outside = tmp_path / "outside"
    outside.mkdir()

    def lookup(path: Path, **query: Any) -> tuple[int, Any]:
        text = urllib.parse.urlencode({"path": str(path), **query})
        return client.request("GET", f"/report?{text}")

    with _serve(tmp_path, [work]) as client:
        status, answer = lookup(sample)
        assert status == 200, answer
        assert answer["sample"]["name"] == "HG002" and answer["disclaimer"]
        overview = answer["sections"]["pharmacogenomics"]
        assert overview["summary"] and "findings" not in overview
        status, answer = lookup(sample, section="pharmacogenomics")
        assert [f["gene"] for f in answer["findings"]] == ["CYP2C19"]
        assert lookup(sample, section="nope")[0] == 400
        assert lookup(work)[1]["error"]["code"] == "no_report"
        assert lookup(outside)[1]["error"]["code"] == "path_not_allowed"

        # Review of #487: a dangling report link was a 500; it is skipped.
        try:
            (sample / "HG004_personal_report.json").symlink_to(work / "gone.json")
        except OSError:
            pass
        else:
            status, answer = lookup(sample)
            assert (status, answer["sample"]["name"]) == (200, "HG002"), answer

        # Review of #487: a report whose sections aren't a mapping was a 500.
        report = sample / "HG002_personal_report.json"
        broken = work / "broken_personal_report.json"
        for sections in (["pharmacogenomics"], {"pgx": "text"}):
            document = json.loads(report.read_text())
            broken.write_text(json.dumps({**document, "sections": sections}))
            status, answer = lookup(broken)
            assert (status, answer["error"]["code"]) == (400, "usage"), sections

        # A newer report reached through a link must still be inside.
        report = sample / "HG002_personal_report.json"
        elsewhere = outside / "HG003_personal_report.json"
        shutil.copy(report, elsewhere)
        try:
            (sample / elsewhere.name).symlink_to(elsewhere)
        except OSError:
            return
        os.utime(elsewhere, (time.time() + 60, time.time() + 60))
        status, answer = lookup(sample)
        assert (status, answer["error"]["code"]) == (403, "path_not_allowed")


def test_a_lookup_refuses_a_boolean_it_cannot_read(tmp_path, work):
    # Review of #487: full=maybe quietly meant false.
    manifest = work / "snps.vcf.manifest.json"
    with _serve(tmp_path, [work]) as client:
        for value in ("maybe", "2", "Yes", "0"):
            text = urllib.parse.urlencode({"path": str(manifest), "full": value})
            status, answer = client.request("GET", f"/manifest?{text}")
            # A readable value gets as far as looking for the (absent) file.
            expected = (400, "usage") if value in ("maybe", "2") else (404,)
            assert (status, answer["error"]["code"])[: len(expected)] == expected


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the user",
)
def test_an_unreadable_variant_database_is_a_usage_error(
    tmp_path, work, variant_tables
):
    # Review of #487: opening it raised outside the error handling (a 500).
    db = work / "locked_variants.sqlite"
    shutil.copy(variant_tables["sqlite"], db)
    db.chmod(0)
    try:
        with _serve(tmp_path, [work]) as client:
            text = urllib.parse.urlencode({"path": str(db)})
            status, answer = client.request("GET", f"/variants?{text}")
    finally:
        db.chmod(0o600)
    assert (status, answer["error"]["code"]) == (400, "usage"), answer


def test_a_blob_in_a_variant_database_is_returned_as_text(
    tmp_path, work, variant_tables
):
    # Review of #487: SQLite stores any type in any column; a BLOB made the
    # MCP query_variants call hang with no reply, and HTTP fail.
    db = work / "blob_variants.sqlite"
    shutil.copy(variant_tables["sqlite"], db)
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        con.execute("UPDATE variants SET gene = ? WHERE rsid = 'rs4'", (b"GENEB",))
    mcp = Mcp("--allow", str(work))
    result = mcp.call("query_variants", path=str(db), rsid="rs4")
    assert result["isError"] is False
    assert [r["gene"] for r in result["structuredContent"]["rows"]] == ["GENEB"]
    assert mcp.close() == 0
    with _serve(tmp_path, [work]) as client:
        text = urllib.parse.urlencode({"path": str(db), "rsid": "rs4"})
        status, answer = client.request("GET", f"/variants?{text}")
    assert (status, [r["gene"] for r in answer["rows"]]) == (200, ["GENEB"])


def test_mcp_offers_lookups_without_allow_jobs(work, variant_tables):
    mcp = Mcp("--allow", str(work))
    tools = {t["name"]: t for t in mcp.request("tools/list", {})["result"]["tools"]}
    for name in ("read_manifest", "query_variants", "report_section"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    result = mcp.call("query_variants", path=str(variant_tables["sqlite"]), rsid="rs4")
    assert result["isError"] is False
    assert [r["gene"] for r in result["structuredContent"]["rows"]] == ["GENEB"]
    result = mcp.call("query_variants", path=str(variant_tables["tsv"]), limit="2")
    assert result["structuredContent"]["error"]["code"] == "usage"
    result = mcp.call("report_section", path=str(work.parent))
    assert result["structuredContent"]["error"]["code"] == "path_not_allowed"
    assert mcp.close() == 0
