"""NVIDIA Parabricks: GPU versions of BWA-MEM, DeepVariant and HaplotypeCaller.

Parabricks ships as a Docker image. The CLI doesn't install it; it checks for
Docker, a visible NVIDIA GPU, Docker's NVIDIA runtime and the image, and says
what's missing. Tools run in a container that sees the input, output and
reference directories at their own paths, as the calling user, so paths in
arguments need no translation and outputs aren't owned by root.
"""

import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

DEFAULT_IMAGE = "nvcr.io/nvidia/clara/clara-parabricks:4.8.0-1"
_SETUP = (
    "Parabricks needs Linux (or WSL2) with an NVIDIA GPU (16 GB+), Docker, the "
    "NVIDIA Container Toolkit, and its image: docker pull {image}"
)


def parabricks_image() -> str:
    return str(settings.get("parabricks_image") or DEFAULT_IMAGE)


def _docker() -> str:
    docker = get_tool_path("docker")
    if not docker:
        raise WGSExtractError(
            "Docker not found. " + _SETUP.format(image=parabricks_image())
        )
    return docker


def verify_parabricks() -> None:
    """Raise WGSExtractError naming the first missing piece of a Parabricks setup."""
    image = parabricks_image()
    if not sys.platform.startswith("linux"):
        raise WGSExtractError(
            "Parabricks runs on Linux only. " + _SETUP.format(image=image)
        )
    docker = _docker()
    if not get_tool_path("nvidia-smi"):
        raise WGSExtractError(
            "No NVIDIA GPU driver found (nvidia-smi). " + _SETUP.format(image=image)
        )
    info = run_command(
        [docker, "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        check=False,
    )
    if info.returncode != 0:
        raise WGSExtractError(
            f"Docker isn't usable: {(info.stderr or '').strip()}. Start the Docker "
            "service, and add your user to the docker group (then log in again)."
        )
    if "nvidia" not in (info.stdout or ""):
        raise WGSExtractError(
            "Docker has no NVIDIA runtime: install the NVIDIA Container Toolkit, "
            "then run 'sudo nvidia-ctk runtime configure --runtime=docker' and "
            "restart Docker."
        )
    if (
        run_command(
            [docker, "image", "inspect", image], capture_output=True, check=False
        ).returncode
        != 0
    ):
        raise WGSExtractError(
            f"The Parabricks image isn't downloaded: docker pull {image} "
            "(set parabricks_image in config.toml for another version)."
        )


def host_path(path: str) -> str:
    """The absolute path a file has both outside and inside the container."""
    return os.path.abspath(path)


def run_pbrun(tool: str, args: list[str], paths: Iterable[str | None]) -> None:
    """Run `pbrun <tool> <args>` in the Parabricks container. ``paths`` are the
    files and directories the run reads or writes, and ``args`` name them by
    host_path; their directories are mounted at the same paths in the container."""
    mounts = sorted(
        {host_path(p) if Path(p).is_dir() else str(Path(host_path(p)).parent)
         for p in paths if p}
    )  # fmt: skip
    cmd = [_docker(), "run", "--rm", "--gpus", "all"]
    cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
    for mount in mounts:
        cmd += ["-v", f"{mount}:{mount}"]
    cmd += ["-w", mounts[0], parabricks_image(), "pbrun", tool, *args]
    logging.info(f"Running Parabricks {tool} on the GPU...")
    try:
        run_command(cmd)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Parabricks {tool} failed: {e}") from e


def call_variants(
    tool: str,
    input_path: str,
    ref: str,
    out_vcf: str,
    region: str | None,
    extra: list[str] | None = None,
) -> None:
    """GPU variant calling (pbrun deepvariant or haplotypecaller) into out_vcf;
    a comma-separated --region becomes one interval each."""
    intervals = [arg for r in (region or "").split(",") if r for arg in ("-L", r)]
    with tempfile.TemporaryDirectory(
        dir=str(Path(out_vcf).parent), prefix=".wgse_pb_"
    ) as tmp:
        run_pbrun(
            tool,
            ["--ref", host_path(ref), "--in-bam", host_path(input_path)]
            + ["--out-variants", host_path(out_vcf), "--tmp-dir", host_path(tmp)]
            + intervals
            + (extra or []),
            [ref, input_path, out_vcf, tmp],
        )
