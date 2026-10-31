"""NVIDIA Parabricks: GPU versions of BWA-MEM, DeepVariant and HaplotypeCaller.

Parabricks ships as a Docker image (see containers). The CLI checks for Docker,
a visible NVIDIA GPU, Docker's NVIDIA runtime and the image, and says what's
missing.
"""

import logging
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.containers import (
    docker,
    docker_info,
    require_image,
    run_container,
)
from wgsextract_cli.core.containers import (
    host_path as host_path,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

DEFAULT_IMAGE = "nvcr.io/nvidia/clara/clara-parabricks:4.8.0-1"
_SETUP = (
    "Parabricks needs Linux (or WSL2) with an NVIDIA GPU (16 GB+), Docker, the "
    "NVIDIA Container Toolkit, and its image: docker pull {image}"
)


def parabricks_image() -> str:
    return str(settings.get("parabricks_image") or DEFAULT_IMAGE)


def verify_parabricks() -> None:
    """Raise WGSExtractError naming the first missing piece of a Parabricks setup."""
    image = parabricks_image()
    setup = _SETUP.format(image=image)
    if not sys.platform.startswith("linux"):
        raise WGSExtractError(f"Parabricks runs on Linux only. {setup}")
    docker(setup)
    if not get_tool_path("nvidia-smi"):
        raise WGSExtractError(f"No NVIDIA GPU driver found (nvidia-smi). {setup}")
    if "nvidia" not in docker_info(setup, "{{json .Runtimes}}"):
        raise WGSExtractError(
            "Docker has no NVIDIA runtime: install the NVIDIA Container Toolkit, "
            "then run 'sudo nvidia-ctk runtime configure --runtime=docker' and "
            "restart Docker."
        )
    require_image(
        image,
        f"The Parabricks image isn't downloaded: docker pull {image} "
        "(set parabricks_image in config.toml for another version).",
    )


def run_pbrun(tool: str, args: list[str], paths: Iterable[str | None]) -> None:
    """Run `pbrun <tool> <args>` in the Parabricks container (see run_container)."""
    logging.info(f"Running Parabricks {tool} on the GPU...")
    try:
        run_container(parabricks_image(), ["pbrun", tool, *args], paths, gpus=True)
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
