"""NVIDIA Parabricks: GPU versions of BWA-MEM, DeepVariant and HaplotypeCaller.

Parabricks ships as a Docker image (see containers). The CLI checks for Docker,
a visible NVIDIA GPU, Docker's NVIDIA runtime and the image, and says what's
missing.
"""

import logging
import os
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
from wgsextract_cli.core.native_support import require_native_tool
from wgsextract_cli.core.region_selection import region_items
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
)

DEFAULT_IMAGE = "nvcr.io/nvidia/clara/clara-parabricks:4.8.0-1"
_SETUP = (
    "Parabricks needs Linux (or WSL2) with an NVIDIA GPU (16 GB+), Docker, the "
    "NVIDIA Container Toolkit, and its image: docker pull {image}"
)


def parabricks_image() -> str:
    return str(settings.get("parabricks_image") or DEFAULT_IMAGE)


def verify_parabricks() -> None:
    """Name the first missing piece of a Parabricks setup: a usage error off
    Linux, a missing dependency (exit 3) for Docker, the driver or the image."""
    require_native_tool("parabricks")
    image = parabricks_image()
    setup = _SETUP.format(image=image)
    if not sys.platform.startswith("linux"):
        raise UsageError(f"Parabricks runs on Linux only. {setup}")
    docker(setup)
    if not get_tool_path("nvidia-smi"):
        raise MissingDependencyError(
            f"No NVIDIA GPU driver found (nvidia-smi). {setup}"
        )
    if "nvidia" not in docker_info(setup, "{{json .Runtimes}}"):
        raise MissingDependencyError(
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
    require_native_tool("parabricks")
    logging.info(f"Running Parabricks {tool} on the GPU...")
    try:
        run_container(parabricks_image(), ["pbrun", tool, *args], paths, gpus=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Parabricks {tool} failed: {e}") from e


def _link_chain(path: str) -> list[str]:
    """``path`` and, if it is a symlink, each file it leads to. pbrun takes an
    interval file's format from its name, so a link (``panel.bed`` to a store's
    ``dataset_7.dat``) is passed as itself, and every folder on the way to its
    target is mounted so the link still opens in the container."""
    chain = [path]
    while Path(chain[-1]).is_symlink() and len(chain) <= 40:
        link = chain[-1]
        chain.append(host_path(os.path.join(os.path.dirname(link), os.readlink(link))))
    target = str(Path(path).resolve())
    return chain if target in chain else [*chain, target]


def call_variants(
    tool: str,
    input_path: str,
    ref: str,
    out_vcf: str,
    region: str | None,
    extra: list[str] | None = None,
) -> None:
    """GPU variant calling (pbrun deepvariant or haplotypecaller) into out_vcf;
    a comma-separated --region becomes one interval each, and a BED file in
    it an --interval-file (pbrun's -L takes only literal intervals)."""
    require_native_tool("parabricks")
    regions = [
        host_path(r) if Path(r).is_file() else r
        for r in region_items(region or "", commas=True)
    ]
    beds = [r for r in regions if Path(r).is_file()]
    intervals = [
        arg
        for r in regions
        for arg in (("--interval-file", host_path(r)) if r in beds else ("-L", r))
    ]
    with tempfile.TemporaryDirectory(
        dir=str(Path(out_vcf).parent), prefix=".wgse_pb_"
    ) as tmp:
        run_pbrun(
            tool,
            ["--ref", host_path(ref), "--in-bam", host_path(input_path)]
            + ["--out-variants", host_path(out_vcf), "--tmp-dir", host_path(tmp)]
            + intervals
            + (extra or []),
            [ref, input_path, out_vcf, tmp, *(p for b in beds for p in _link_chain(b))],
        )
