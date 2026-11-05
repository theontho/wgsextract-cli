"""Running tools from Docker images: Parabricks on a GPU, Google's DeepVariant.

The CLI doesn't install Docker or pull images; it checks for them and says
what's missing. A tool runs in a container that sees the files it reads and
writes at their own paths, as the calling user, so paths in arguments need no
translation and outputs aren't owned by root.
"""

import logging
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    WGSExtractError,
    popen,
    run_command,
)

# Seconds a failed container's OOM event may trail its exit.
OOM_EVENT_GRACE = 5.0


def docker(setup: str) -> str:
    path = get_tool_path("docker")
    if not path:
        raise MissingDependencyError(f"Docker not found. {setup}")
    return path


def docker_info(setup: str, field: str = "{{json .ServerVersion}}") -> str:
    """`docker info --format <field>`, raising when the daemon isn't usable."""
    info = run_command(
        [docker(setup), "info", "--format", field], capture_output=True, check=False
    )
    if info.returncode != 0:
        raise MissingDependencyError(
            f"Docker isn't usable: {(info.stderr or '').strip()}. Start the Docker "
            "service, and add your user to the docker group (then log in again)."
        )
    return info.stdout or ""


def require_image(image: str, missing: str) -> None:
    inspect = [docker(missing), "image", "inspect", image]
    if run_command(inspect, capture_output=True, check=False).returncode != 0:
        raise MissingDependencyError(missing)


def host_path(path: str) -> str:
    """The absolute path a file has both outside and inside the container."""
    return os.path.abspath(path)


def run_container(
    image: str,
    command: list[str],
    paths: Iterable[str | None],
    *,
    gpus: bool = False,
    entrypoint: str | None = None,
    temporary_directory: str | None = None,
    local_only: bool = False,
    oom_error: str | None = None,
) -> None:
    """Run ``command`` in ``image``. ``paths`` are the files and directories it
    reads or writes, and ``command`` names them by host_path; their directories
    are mounted at the same paths in the container.

    With ``oom_error``, an out-of-memory kill of any process in the container
    stops it and raises that message, for tools such as DeepVariant whose
    worker pools wait forever on a killed worker."""
    mounts = sorted(
        {host_path(p) if Path(p).is_dir() else str(Path(host_path(p)).parent)
         for p in paths if p}
    )  # fmt: skip
    cmd = [docker(""), "run", "--rm", *(["--gpus", "all"] if gpus else [])]
    if local_only:
        cmd += ["--pull=never"]
    if entrypoint is not None:
        cmd += ["--entrypoint", entrypoint]
    # The user has no home in the image; tools that write settings use /tmp.
    # Docker Desktop on Windows maps file ownership itself and has no POSIX ids.
    if sys.platform != "win32":
        cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
    home = host_path(temporary_directory) if temporary_directory else "/tmp"
    cmd += ["-e", f"HOME={home}"]
    if temporary_directory:
        cmd += ["-e", f"TMPDIR={home}"]
    for mount in mounts:
        cmd += ["-v", f"{mount}:{mount}"]
    if oom_error is None:
        run_command([*cmd, "-w", mounts[0], image, *command])
        return
    name = f"wgsextract-{uuid.uuid4().hex[:12]}"
    with _oom_watch(cmd[0], name) as oom_killed:
        try:
            run_command([*cmd, "--name", name, "-w", mounts[0], image, *command])
        except subprocess.CalledProcessError as error:
            if oom_killed.wait(OOM_EVENT_GRACE):
                raise WGSExtractError(oom_error) from error
            raise


@contextmanager
def _oom_watch(docker_path: str, name: str) -> Iterator[threading.Event]:
    """Set the yielded event, and kill container ``name``, on its first OOM kill.

    Docker reports an "oom" event when the container's cgroup records an OOM
    kill, whether from a --memory cap or the host running out of memory.
    """
    killed = threading.Event()
    try:
        events = popen(
            [docker_path, "events", "--filter", f"container={name}",
             "--filter", "event=oom", "--format", "{{.Action}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )  # fmt: skip
    except OSError as error:
        logging.warning(f"Out-of-memory kills won't be detected: {error}")
        yield killed
        return

    def watch() -> None:
        assert events.stdout is not None
        for _line in events.stdout:
            if killed.is_set():
                continue
            killed.set()
            logging.error(f"A process in container {name} ran out of memory.")
            run_command([docker_path, "kill", name], capture_output=True, check=False)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield killed
    finally:
        events.terminate()
        try:
            events.wait(timeout=5)
        except subprocess.TimeoutExpired:
            events.kill()
