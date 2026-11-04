"""run_container's OOM watcher against a fake docker CLI."""

import os
import subprocess
import sys
import threading
import time

import pytest

from wgsextract_cli.core import containers
from wgsextract_cli.core.utils import WGSExtractError

# `run` blocks like a container whose worker pool waits on a killed worker;
# `events` reports an OOM kill once it is running; `kill` stops it.
FAKE_DOCKER = """#!/bin/sh
state="$(dirname "$0")"
case "$1" in
  run)
    echo $$ > "$state/run.pid"
    exec sleep 60 ;;
  events)
    [ -e "$state/no-oom" ] && exec sleep 60
    while [ ! -e "$state/run.pid" ]; do sleep 0.05; done
    echo oom
    exec sleep 60 ;;
  kill)
    echo "$2" > "$state/killed"
    kill "$(cat "$state/run.pid")" ;;
esac
"""


@pytest.fixture
def docker(tmp_path, monkeypatch):
    path = tmp_path / "docker"
    path.write_text(FAKE_DOCKER)
    path.chmod(0o755)
    monkeypatch.setattr(containers, "get_tool_path", lambda _tool: str(path))
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_oom_kill_stops_a_hung_container_with_the_given_error(docker):
    started = time.monotonic()
    with pytest.raises(WGSExtractError, match="^out of memory$"):
        containers.run_container(
            "image", ["tool"], [str(docker)], oom_error="out of memory"
        )
    assert time.monotonic() - started < 30
    assert (docker / "killed").read_text().startswith("wgsextract-")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fake")
def test_other_failures_keep_their_error(docker, monkeypatch):
    monkeypatch.setattr(containers, "OOM_EVENT_GRACE", 0.1)
    (docker / "no-oom").touch()

    def stop_run():
        while not (docker / "run.pid").exists():
            time.sleep(0.05)
        os.kill(int((docker / "run.pid").read_text()), 15)

    threading.Thread(target=stop_run, daemon=True).start()
    with pytest.raises(subprocess.CalledProcessError) as error:
        containers.run_container("image", ["tool"], [str(docker)], oom_error="oom")
    assert "oom" not in str(error.value)
    assert not (docker / "killed").exists()
