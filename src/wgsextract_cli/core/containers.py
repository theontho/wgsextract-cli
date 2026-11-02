"""Running tools from Docker images: Parabricks on a GPU, Google's DeepVariant.

The CLI doesn't install Docker or pull images; it checks for them and says
what's missing. A tool runs in a container that sees the files it reads and
writes at their own paths, as the calling user, so paths in arguments need no
translation and outputs aren't owned by root.
"""

import os
import sys
from collections.abc import Iterable
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command


def docker(setup: str) -> str:
    path = get_tool_path("docker")
    if not path:
        raise WGSExtractError(f"Docker not found. {setup}")
    return path


def docker_info(setup: str, field: str = "{{json .ServerVersion}}") -> str:
    """`docker info --format <field>`, raising when the daemon isn't usable."""
    info = run_command(
        [docker(setup), "info", "--format", field], capture_output=True, check=False
    )
    if info.returncode != 0:
        raise WGSExtractError(
            f"Docker isn't usable: {(info.stderr or '').strip()}. Start the Docker "
            "service, and add your user to the docker group (then log in again)."
        )
    return info.stdout or ""


def require_image(image: str, missing: str) -> None:
    inspect = [docker(missing), "image", "inspect", image]
    if run_command(inspect, capture_output=True, check=False).returncode != 0:
        raise WGSExtractError(missing)


def host_path(path: str) -> str:
    """The absolute path a file has both outside and inside the container."""
    return os.path.abspath(path)


def run_container(
    image: str, command: list[str], paths: Iterable[str | None], *, gpus: bool = False
) -> None:
    """Run ``command`` in ``image``. ``paths`` are the files and directories it
    reads or writes, and ``command`` names them by host_path; their directories
    are mounted at the same paths in the container."""
    mounts = sorted(
        {host_path(p) if Path(p).is_dir() else str(Path(host_path(p)).parent)
         for p in paths if p}
    )  # fmt: skip
    cmd = [docker(""), "run", "--rm", *(["--gpus", "all"] if gpus else [])]
    # The user has no home in the image; tools that write settings use /tmp.
    # Docker Desktop on Windows maps file ownership itself and has no POSIX ids.
    if sys.platform != "win32":
        cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
    cmd += ["-e", "HOME=/tmp"]
    for mount in mounts:
        cmd += ["-v", f"{mount}:{mount}"]
    run_command([*cmd, "-w", mounts[0], image, *command])
