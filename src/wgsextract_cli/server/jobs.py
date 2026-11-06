"""Jobs: each request runs in its own worker process, so long operations run in
the background, can be cancelled, and survive in the job list across server
restarts.

A job's record is ``<state>/server/jobs/<id>.json``, its worker's log
``<id>.log``. The worker (``python -m wgsextract_cli.server.worker``) reads
the request from stdin, writes progress and the result as JSON lines to
stdout, and cancels the operation when it reads ``cancel`` or stdin closes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from wgsextract_cli.core.file_lock import exclusive_lock
from wgsextract_cli.core.utils import WGSExtractError

from .operations import Operation, RequestError, is_network_path

Status = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
FINISHED = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
RECORD_SCHEMA = "wgsextract.job/1"

# How long a cancelled worker may take to stop its tools and write its
# manifest before it is killed.
CANCEL_GRACE_SECONDS = 30.0
# A cancelled job's exit code, however it stopped: as for Ctrl-C on the CLI.
CANCELLED_EXIT_CODE = 130
# Result fields left out of compact job views: the manifest has them.
_VERBOSE_RESULT_FIELDS = ("provenance", "schema")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    op: str
    kind: str
    params: dict[str, Any]
    status: Status = "queued"
    created: str = field(default_factory=_now)
    started: str | None = None
    finished: str | None = None
    exit_code: int | None = None
    error: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    cancel_requested: str | None = None
    server_pid: int = field(default_factory=os.getpid)
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def done(self) -> bool:
        return self.status in FINISHED

    def record(self) -> dict[str, Any]:
        """What is saved: everything but the event history."""
        data = {k: v for k, v in vars(self).items() if k != "events"}
        return {"schema": RECORD_SCHEMA, **data}

    def view(self, *, full: bool = False) -> dict[str, Any]:
        """The job as a client sees it: unset fields left out and, unless
        ``full``, the result without its provenance."""
        hidden = {"events", "server_pid"} | (set() if full else {"params"})
        data = {
            k: v for k, v in vars(self).items() if k not in hidden and v is not None
        }
        if self.result is not None and not full:
            data["result"] = {
                k: v
                for k, v in self.result.items()
                if k not in _VERBOSE_RESULT_FIELDS and v not in (None, [], {})
            }
        return data


class JobManager:
    """Starts, tracks and cancels jobs. Thread-safe.

    Queries start at once; at most ``max_jobs`` other jobs run at a time and
    the rest wait in order. Jobs writing to the same folder run one at a time,
    since commands save fixed file names there. One manager owns a job folder
    at a time (an exclusive lock), so no two servers share their jobs.
    """

    def __init__(
        self,
        directory: Path,
        *,
        config_path: Path | None,
        max_jobs: int = 1,
        debug: bool = False,
        default_outdir: str | None = None,
        worker_command: list[str] | None = None,
    ) -> None:
        self.directory = directory
        self.default_outdir = default_outdir
        self.config_path = config_path
        self.max_jobs = max(1, max_jobs)
        self.debug = debug
        self.worker_command = worker_command or [
            sys.executable,
            "-m",
            "wgsextract_cli.server.worker",
        ]
        self._lock = threading.Condition()
        self._jobs: dict[str, Job] = {}
        self._queue: list[str] = []
        self._workers: dict[str, subprocess.Popen[str]] = {}
        # Folders each queued or running job writes in, for _pump.
        self._writes: dict[str, frozenset[str]] = {}
        self._cancelling: set[str] = set()
        self._stopping = False
        directory.mkdir(parents=True, exist_ok=True)
        # Job records and logs hold results and paths: this user's only.
        if sys.platform != "win32":
            directory.chmod(0o700)
        self._owner = contextlib.ExitStack()
        try:
            self._owner.enter_context(exclusive_lock(directory / "owner", timeout=0))
        except WGSExtractError:
            raise WGSExtractError(
                f"Another wgsextract server is using {directory}; stop it first."
            ) from None
        self._load()

    # Records ---------------------------------------------------------------

    def _load(self) -> None:
        jobs = []
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data.pop("schema", None)
                job = Job(**data)
            except (OSError, ValueError, TypeError) as e:
                logging.warning(f"Ignoring unreadable job record {path}: {e}")
                continue
            # This manager holds the folder's lock, so no other server is
            # running these jobs any more.
            if not job.done:
                job.status = "interrupted"
                job.finished = job.finished or _now()
                job.error = {
                    "kind": "cancelled",
                    "message": "The server stopped before the job finished; "
                    "rerun it to resume from its completed steps.",
                }
                self._save(job)
            jobs.append(job)
        for job in sorted(jobs, key=lambda j: j.created):
            self._jobs[job.id] = job

    def _save(self, job: Job) -> None:
        path = self.directory / f"{job.id}.json"
        staged = path.with_suffix(".json.tmp")
        staged.write_text(
            json.dumps(job.record(), indent=1), encoding="utf-8", newline="\n"
        )
        os.replace(staged, path)

    def log_path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.log"

    # Queries ---------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def wait(self, job_id: str, timeout: float) -> Job | None:
        """The job once finished, or as it is after ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        with self._lock:
            job = self._jobs.get(job_id)
            while job is not None and not job.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
            return job

    def events(
        self, job_id: str, after: int, timeout: float
    ) -> tuple[list[dict[str, Any]], bool]:
        """Events with ``seq`` above ``after``, waiting up to ``timeout`` for
        one; and whether the job has finished."""
        deadline = time.monotonic() + timeout
        with self._lock:
            job = self._jobs[job_id]
            while True:
                new = [e for e in job.events if e["seq"] > after]
                remaining = deadline - time.monotonic()
                if new or job.done or remaining <= 0:
                    return new, job.done
                self._lock.wait(remaining)

    # Changes ---------------------------------------------------------------

    def submit(self, op: Operation, params: Mapping[str, Any]) -> Job:
        job = Job(
            id=time.strftime("%Y%m%d-%H%M%S-", time.gmtime()) + secrets.token_hex(3),
            op=op.name,
            kind=op.kind,
            params=dict(params),
        )
        names = {f.name for f in op.fields()}
        # With no input to save beside, outputs would go to the working folder.
        if (
            op.kind != "query"
            and "outdir" in names
            and not names & set(_INPUT_NAMES)
            and not op.params.WRITES_BESIDE
            and not self._output_folder(op, job)
        ):
            raise RequestError(
                400,
                "usage",
                "'outdir' is required: with no input or configured output "
                "folder, this job would save into the server's working folder.",
            )
        with self._lock:
            if self._stopping:
                raise RequestError(503, "stopping", "The server is stopping.")
            self._jobs[job.id] = job
            self._writes[job.id] = self._write_folders(op, job)
            self._event(job, "status", status="queued")
            self._save(job)
            if op.kind == "query":
                self._start(job)
            else:
                self._queue.append(job.id)
                self._pump()
        return job

    def cancel(self, job_id: str) -> Job | None:
        """Cancel a job: a queued one at once, a running one in the background
        (its tools are stopped and it finishes as cancelled)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.done:
                return job
            if job.id in self._queue:
                self._queue.remove(job.id)
                self._finish(job, "cancelled", error="Cancelled before it started.")
                return job
            worker = self._workers.get(job.id)
            if worker is not None and job.cancel_requested is None:
                job.cancel_requested = _now()
                self._event(job, "status", status="cancelling")
                threading.Thread(
                    target=self._stop_worker, args=(job.id, worker), daemon=True
                ).start()
            return job

    def shutdown(self) -> None:
        """Stop: queued jobs never start and running ones are cancelled; both
        are recorded as interrupted."""
        with self._lock:
            self._stopping = True
            for job_id in self._queue:
                self._finish(
                    self._jobs[job_id], "interrupted", error="The server stopped."
                )
            self._queue.clear()
            workers = dict(self._workers)
        threads = [
            threading.Thread(target=self._stop_worker, args=item)
            for item in workers.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with self._lock:
            # Let the followers record each job's final status.
            self._lock.wait_for(lambda: not self._workers, timeout=10)
        self._owner.close()

    # Internals (called with the lock held unless noted) --------------------

    def _event(self, job: Job, kind: str, **data: Any) -> None:
        seq = job.events[-1]["seq"] + 1 if job.events else 1
        job.events.append({"seq": seq, "type": kind, "time": _now(), **data})
        self._lock.notify_all()

    def _pump(self) -> None:
        active = [self._jobs[i] for i in self._workers if self._jobs[i].kind != "query"]
        busy = set().union(*(self._writes.get(job.id, ()) for job in active))
        for job_id in list(self._queue):
            if len(active) >= self.max_jobs or self._stopping:
                break
            job = self._jobs[job_id]
            folders = self._writes.get(job_id, frozenset())
            if folders & busy:
                continue
            self._queue.remove(job_id)
            if self._start(job):
                active.append(job)
                busy |= folders

    def _output_folder(self, op: Operation, job: Job) -> str:
        """Where a job saves its files: its outdir, else the configured output
        folder, else its first input's folder; for an operation that writes
        beside a path, its outdir, else that path's folder."""
        folder = job.params.get("outdir")
        if not folder and op.params.WRITES_BESIDE:
            folder = _beside_folder(op, job.params)
        elif not folder:
            folder = self.default_outdir or os.path.dirname(_first_input(job.params))
        return _folder_key(folder) if folder else ""

    def _write_folders(self, op: Operation, job: Job) -> frozenset[str]:
        """Every folder a job may write in: its output folder, the folders of
        output files it names by path, and a folder given as its input (a
        report folder is written into, not beside)."""
        folders = {self._output_folder(op, job)}
        for item in op.fields():
            value = job.params.get(item.name)
            if not isinstance(value, str) or not value:
                continue
            if item.spec.may_name_file and os.path.dirname(value):
                folders.add(_folder_key(os.path.dirname(value)))
            elif (
                item.name in _INPUT_NAMES
                and not is_network_path(value)
                and os.path.isdir(value)
            ):
                folders.add(_folder_key(value))
        return frozenset(folders)

    def _start(self, job: Job) -> bool:
        """Start a job's worker; False if it could not start (job failed)."""
        request = {
            "op": job.op,
            "params": job.params,
            "config": str(self.config_path) if self.config_path else None,
            "debug": self.debug,
        }
        log = open(self.log_path(job.id), "a", encoding="utf-8", newline="\n")  # noqa: SIM115
        try:
            worker = subprocess.Popen(
                self.worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                encoding="utf-8",
                errors="replace",
                **_detached(),
            )
        except OSError as e:
            log.close()
            self._finish(job, "failed", error=f"Could not start a worker: {e}")
            return False
        assert worker.stdin is not None
        worker.stdin.write(json.dumps(request) + "\n")
        worker.stdin.flush()
        self._workers[job.id] = worker
        job.status = "running"
        job.started = _now()
        self._event(job, "status", status="running")
        self._save(job)
        threading.Thread(
            target=self._follow, args=(job, worker, log), daemon=True
        ).start()
        return True

    def _follow(self, job: Job, worker: subprocess.Popen[str], log: Any) -> None:
        """Read a worker's events until it exits (runs without the lock)."""
        assert worker.stdout is not None
        result: dict[str, Any] | None = None
        for line in worker.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if not isinstance(message, dict) or "event" not in message:
                log.write(line)
                log.flush()
                continue
            if message["event"] == "progress":
                with self._lock:
                    progress = {
                        k: message.get(k)
                        for k in ("stage", "message", "fraction")
                        if message.get(k) is not None
                    }
                    # Tools often run in many short steps with the same message.
                    if progress != job.progress:
                        job.progress = progress
                        self._event(job, "progress", **progress)
            elif message["event"] == "result":
                result = message.get("result")
        code = worker.wait()
        log.close()
        with self._lock:
            self._workers.pop(job.id, None)
            if worker.stdin:
                with contextlib.suppress(OSError):
                    worker.stdin.close()
            if isinstance(result, dict):
                status: Status = {
                    "ok": "succeeded",
                    "cancelled": "cancelled",
                }.get(str(result.get("status")), "failed")  # type: ignore[assignment]
                if self._stopping and status == "cancelled":
                    status = "interrupted"
                job.result = result
                job.exit_code = result.get("exit_code", code)
                self._finish(job, status, error_record=result.get("error"))
            else:
                job.exit_code = code
                cancelled = job.id in self._cancelling
                self._finish(
                    job,
                    "interrupted"
                    if self._stopping
                    else "cancelled"
                    if cancelled
                    else "failed",
                    error=(
                        "Stopped before writing a result."
                        if cancelled or self._stopping
                        else f"The worker exited ({code}) without a result; "
                        f"see GET /v1/jobs/{job.id}/log."
                    ),
                )
            self._cancelling.discard(job.id)
            self._pump()

    def _stop_worker(self, job_id: str, worker: subprocess.Popen[str]) -> None:
        """Ask a worker to cancel; kill it after the grace period (no lock)."""
        with self._lock:
            self._cancelling.add(job_id)
        try:
            assert worker.stdin is not None
            worker.stdin.write("cancel\n")
            worker.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            worker.wait(CANCEL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logging.warning(f"Job {job_id} did not stop; killing its worker.")
            _kill_tree(worker.pid)

    def _finish(
        self,
        job: Job,
        status: Status,
        *,
        error: str | None = None,
        error_record: Mapping[str, Any] | None = None,
    ) -> None:
        job.status = status
        job.finished = _now()
        if status == "cancelled":
            job.exit_code = CANCELLED_EXIT_CODE
        if error_record:
            job.error = dict(error_record)
        elif error:
            job.error = {
                "kind": "failure" if status == "failed" else "cancelled",
                "message": error,
            }
        self._writes.pop(job.id, None)
        self._event(job, "status", status=status)
        self._save(job)


def _detached() -> dict[str, Any]:
    """Run workers in their own process group: a terminal's Ctrl-C stops the
    server, which then cancels them cleanly."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(pid: int) -> None:
    import psutil

    with contextlib.suppress(psutil.Error):
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
        parent.kill()


def write_private_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` readable by this user only (it holds the token)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1)


# In precedence order: a trio's proband overrides its other inputs.
_INPUT_NAMES = ("proband", "vcf_input", "input", "inputs", "r1")


def _folder_key(folder: str) -> str:
    folder = os.path.expanduser(folder)
    # Requests were checked, but never open a network path (see PathGuard).
    if not is_network_path(folder):
        folder = os.path.realpath(folder)
    return os.path.normcase(folder)


def _beside_folder(op: Operation, params: Mapping[str, Any]) -> str:
    """The folder an operation writes in beside its ``WRITES_BESIDE`` path
    (the request's, else the configured one): the path's own when it is a
    folder."""
    from wgsextract_cli.core.config import settings

    name = op.params.WRITES_BESIDE
    value = params.get(name)
    if not value:
        item = next((f for f in op.fields() if f.name == name), None)
        key = item.spec.config_key if item else None
        value = settings.get(key) if key else None
    if not isinstance(value, str) or not value:
        return ""
    if not is_network_path(value) and os.path.isdir(os.path.expanduser(value)):
        return value
    return os.path.dirname(value)


def _first_input(params: Mapping[str, Any]) -> str:
    """The input a job's outputs go beside when it names no folder."""
    for name in _INPUT_NAMES:
        value = params.get(name)
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, str) and value:
            return value
    return ""
