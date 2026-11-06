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

import psutil

from wgsextract_cli.core import alignment_memory
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
from wgsextract_cli.core.resource_estimates import (
    GIB,
    GpuMemory,
    Operation,
    ParabricksPlan,
    report_resources,
)
from wgsextract_cli.core.results import warn
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


def gpu_memory() -> tuple[GpuMemory, ...]:
    """Each GPU's total and free memory from nvidia-smi; none if it can't say."""
    smi = get_tool_path("nvidia-smi")
    query = "--query-gpu=index,name,memory.total,memory.free"
    try:
        out = subprocess.run(
            [smi or "nvidia-smi", query, "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logging.warning(f"Couldn't read the GPUs' memory from nvidia-smi: {e}")
        return ()

    def mib(value: str) -> int | None:
        return int(value) * 1024**2 if value.strip().isdigit() else None

    gpus = []
    for line in out.splitlines():
        # A GPU's name may hold commas; its index leads and its memory ends.
        index, _, rest = line.partition(",")
        name, total, free = (rest.rsplit(",", 2) + ["", ""])[:3]
        if not index.strip().isdigit():
            continue
        gpus.append(GpuMemory(int(index), name.strip(), mib(total), mib(free)))
    return tuple(gpus)


def plan_gpu_memory(
    tool: str,
    operation: Operation,
    *,
    check: bool = True,
    report: bool = True,
) -> list[str]:
    """Check that the GPUs and the host have the memory Parabricks ``tool``
    needs and return the pbrun options that fit it to them: its low-memory
    option when the GPUs have less free memory than its default needs.
    Refuses a run that can't fit unless ``check`` is off
    (--no-gpu-memory-check); warns when other programs hold the RAM it needs.
    ``report`` logs the plan and records its choices on the result."""
    gpus = gpu_memory()
    plan = ParabricksPlan(
        tool,
        gpus,
        host_total=int(psutil.virtual_memory().total),
        host_available=alignment_memory.available_memory(),
    )
    if not gpus:
        logging.warning("GPU memory is unknown, so Parabricks runs with its defaults.")
    problems = plan.shortfalls()
    if problems and check:
        raise WGSExtractError(
            " ".join(problems) + " Pass --no-gpu-memory-check to try anyway."
        )
    if not report:
        return plan.options
    report_resources(plan.estimate(operation))
    if problems:
        warn(
            "parabricks_memory",
            " ".join(problems) + " Continuing (--no-gpu-memory-check).",
            tool=tool,
        )
    if (short := plan.host_warning()) is not None:
        warn(
            "parabricks_host_memory",
            short,
            tool=tool,
            needed_gib=round(plan.host_needed / GIB, 1),
            available_gib=round(plan.host_available / GIB, 1),
        )
    if plan.low_memory:
        warn(
            "parabricks_low_memory",
            f"Running Parabricks {tool} with {plan.need.option}: the GPU has "
            f"{(plan.gpu_free or 0) / GIB:.1f} GiB free, less than the "
            f"{plan.full_need / GIB:.0f} GiB its default mode needs. It may "
            "run slower.",
            tool=tool,
            option=plan.need.option,
            gpu_free_gib=round((plan.gpu_free or 0) / GIB, 1),
        )
    return plan.options


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
