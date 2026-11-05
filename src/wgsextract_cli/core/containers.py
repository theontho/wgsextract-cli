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
from contextlib import contextmanager, suppress
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.steps import RUN_TAG
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    WGSExtractError,
    popen,
    run_command,
)

# Seconds a failed container's OOM event may trail its exit.
OOM_EVENT_GRACE = 5.0
# Labels each container with the step run (RUN_TAG) that started it.
STEP_LABEL = "wgsextract.step"


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


def image_identity(image: str) -> str:
    """``image`` and the ID of the build it names, so results can tell when a
    mutable tag such as 1.10.0 was replaced; just ``image`` when it's unknown."""
    docker_path = get_tool_path("docker")
    if not docker_path:
        return image
    inspected = run_command(
        [docker_path, "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        check=False,
    )
    identity = (inspected.stdout or "").strip() if inspected.returncode == 0 else ""
    return f"{image}@{identity}" if identity else image


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
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` in ``image``. ``paths`` are the files and directories it
    reads or writes, and ``command`` names them by host_path; their directories
    are mounted at the same paths in the container.

    With ``oom_error``, an out-of-memory kill of any process in the container
    stops it and raises that message, for tools such as DeepVariant whose
    worker pools wait forever on a killed worker. ``capture_output`` returns
    its output instead of showing it."""
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
    name = f"wgsextract-{uuid.uuid4().hex[:12]}"
    cmd += ["--name", name]
    if tag := os.environ.get(RUN_TAG):
        # A rerun finds the container of a step whose wgsextract was killed.
        cmd += ["--label", f"{STEP_LABEL}={tag}"]
    cmd += ["-w", mounts[0], image, *command]
    try:
        if oom_error is None:
            return run_command(cmd, capture_output=capture_output)
        with _oom_watch(cmd[0], name) as oom_killed:
            try:
                return run_command(cmd, capture_output=capture_output)
            except subprocess.CalledProcessError as error:
                if oom_killed.wait(OOM_EVENT_GRACE):
                    raise WGSExtractError(oom_error) from error
                raise
    except (KeyboardInterrupt, SystemExit):
        # Stopping `docker run` leaves its container running, and a shell as
        # the container's PID 1 ignores the signals it forwards. _engine works
        # where the process registry, once cancelled, refuses new processes.
        with suppress(OSError, subprocess.SubprocessError):
            _engine([cmd[0], "kill", name])
        raise


def _engine(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """A quick engine query or command, bounded in case the daemon hangs."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)


def tagged_containers(tags: Iterable[str]) -> list[str]:
    """Running containers that step runs tagged with one of ``tags`` started.
    Empty when there is no container engine, or it doesn't answer."""
    docker_path = get_tool_path("docker")
    if not docker_path:
        return []
    found: list[str] = []
    for tag in sorted(tags):
        try:
            listed = _engine(
                [docker_path, "ps", "--filter", f"label={STEP_LABEL}={tag}",
                 "--format", "{{.Names}}"]
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as error:
            logging.debug(f"Couldn't list containers: {error}")
            return found
        if listed.returncode != 0:
            logging.debug(f"Couldn't list containers: {listed.stderr.strip()}")
            return found
        found += [f"container {name}" for name in listed.stdout.split()]
    return found


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
