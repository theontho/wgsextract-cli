import os
import sys
from threading import Event

import pytest

from wgsextract_cli.core import utils
from wgsextract_cli.core.utils import WGSExtractError, run_pipeline
from wgsextract_cli.core.variant_files import verify_paths_exist

PY = sys.executable


def test_run_pipeline_connects_stages(tmp_path):
    out = tmp_path / "out.txt"
    with out.open("wb") as handle:
        run_pipeline(
            [
                [PY, "-c", "print('a'); print('b')"],
                [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            ],
            stdout=handle,
        )
    assert out.read_text().split() == ["A", "B"]


def test_run_pipeline_reports_failed_upstream_stage_with_stderr():
    with pytest.raises(WGSExtractError) as excinfo:
        run_pipeline(
            [
                [PY, "-c", "import sys; sys.stderr.write('bad input\\n'); sys.exit(3)"],
                [PY, "-c", "import sys; sys.stdin.read()"],
            ],
            stdout=open(os.devnull, "wb"),  # noqa: SIM115
            description="Test pipeline",
        )
    message = str(excinfo.value)
    assert message.startswith("Test pipeline failed")
    assert "exit 3" in message
    assert "bad input" in message


def test_run_pipeline_does_not_deadlock_on_chatty_stderr():
    # Far more stderr than a pipe buffer holds, from a stage nobody reads.
    noisy = "import sys; sys.stderr.write('x' * 2_000_000); print('done')"
    with open(os.devnull, "wb") as devnull:
        run_pipeline(
            [[PY, "-c", noisy], [PY, "-c", "import sys; sys.stdin.read()"]],
            stdout=devnull,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="SIGPIPE is POSIX-only")
def test_run_pipeline_tolerates_sigpipe_when_consumer_succeeds():
    # `yes` is killed by SIGPIPE like a C bio tool; Python would exit 1 instead.
    with open(os.devnull, "wb") as devnull:
        run_pipeline(
            [["yes"], [PY, "-c", "import sys; sys.stdin.readline()"]],
            stdout=devnull,
        )


def test_run_pipeline_requires_a_stage():
    with pytest.raises(ValueError):
        run_pipeline([])


@pytest.mark.parametrize("before_start", [False, True])
def test_pipeline_cancellation_reaps_every_started_stage(monkeypatch, before_start):
    cancelled = Event()
    processes = []
    original_popen = utils.popen
    if before_start:
        cancelled.set()

    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        if len(processes) == 2:
            cancelled.set()
        return process

    monkeypatch.setattr(utils, "popen", capture)
    with pytest.raises(WGSExtractError, match="cancelled"):
        run_pipeline(
            [[PY, "-c", "import time; time.sleep(60)"]] * 2,
            cancel_event=cancelled,
        )
    assert len(processes) == (0 if before_start else 2)
    assert all(process.poll() is not None for process in processes)


def test_verify_paths_exist_raises_with_every_problem(tmp_path):
    existing = tmp_path / "a.bam"
    existing.write_text("")
    verify_paths_exist({"--input": str(existing)})

    with pytest.raises(WGSExtractError) as excinfo:
        verify_paths_exist(
            {
                "--input": str(tmp_path / "missing.bam"),
                "--ref": str(tmp_path),
                "--r1": None,
            }
        )
    message = str(excinfo.value)
    assert "missing.bam" in message
    assert "directory" in message
    assert "--r1" in message
