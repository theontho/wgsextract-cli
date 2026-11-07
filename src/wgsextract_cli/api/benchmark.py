"""``benchmark``: time WGS Extract's operations on one dataset.

It generates a fake BAM (or loads a real dataset), then runs the core suite
(info, reference index, unalign, subsets, SNP calling, microarray) and, for the
``heavy`` suite, the reference, coverage, extraction, QC and VCF steps, each as
a CLI child process. Each run gets a timestamped folder under
``<outdir>/runs`` holding the steps' outputs and logs, ``benchmark_report.md``
and ``benchmark_results.json``; the result lists the two reports. A failed
step fails the operation once the report is written; with ``keep_going`` the
other steps run first. A cancelled run's folder is removed; downloaded
datasets stay cached. Local only: it downloads any dataset URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.commands.benchmark.models import (
    DEFAULT_REAL_DATASET_SHA256,
    DEFAULT_REAL_DATASET_URL,
    PROFILE_DEFAULTS,
    REAL_BENCHMARK_DATASETS,
)
from wgsextract_cli.core.builds import BUILD_CHOICES
from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import memory_param, threads_param


@dataclass(frozen=True, kw_only=True)
class BenchmarkParams(Params):
    """Benchmark the operations on generated or real data (see the module
    docstring). ``coverage``, ``full_size``, ``region`` and ``target_count``
    default to the profile's; ``threads`` overrides the machine's tuned count
    for every step."""

    COMMAND = "benchmark"
    LOCAL_ONLY = True
    SAVES_IN_WORKING_FOLDER = True

    outdir: Path | None = param(
        None,
        doc="Folder for the benchmark runs (default: out/benchmark in the "
        "working folder; config output_directory doesn't apply).",
        flags=("--outdir", "-o"),
        path="directory",
        shared=True,
        writes=True,
    )
    threads: int | None = threads_param()
    memory: str | None = memory_param()
    profile: str = param(
        "standard",
        help=(
            "Benchmark size preset. smoke is chrM-only, standard is a scaled "
            "human-like genome, 200mb targets a ~200 MiB fake BAM, and full "
            "uses real chromosome lengths."
        ),
        choices=tuple(sorted(PROFILE_DEFAULTS)),
    )
    coverage: float | None = param(
        None, help="Override generated BAM coverage for the selected profile."
    )
    full_size: bool = param(
        False,
        help="Use real chromosome lengths for the generated benchmark reference.",
        action="store_true",
    )
    build: str = param(
        "hg38",
        help="Generated reference naming/build convention.",
        choices=tuple(BUILD_CHOICES),
    )
    seed: int = param(
        20260504, help="Random seed for deterministic benchmark data generation."
    )
    target_count: int | None = param(
        None,
        help="Approximate number of target SNPs for the microarray benchmark.",
    )
    region: str | None = param(
        None,
        help="Optional region to benchmark for region-aware operations.",
        flags=("-r", "--region"),
    )
    keep_going: bool = param(
        False,
        help="Continue independent benchmark steps after a failed step when possible.",
        action="store_true",
    )
    suite: str = param(
        "heavy",
        help="Benchmark operation coverage. core runs the lighter baseline suite.",
        choices=("core", "heavy"),
    )
    dataset: str = param(
        "fake",
        help=(
            "Benchmark foundation data. fake generates synthetic data; real uses "
            "the release-backed mini genome zip; real-1x and real-30x cache "
            "public 1000 Genomes WGS references locally."
        ),
        choices=("fake", *sorted(REAL_BENCHMARK_DATASETS)),
    )
    dataset_zip: Path | None = param(
        None,
        help="Local real benchmark dataset zip. Overrides --dataset-url when --dataset real is used.",
        path="file",
    )
    dataset_url: str = param(
        DEFAULT_REAL_DATASET_URL,
        help="URL for the release-backed real benchmark dataset zip used by --dataset real.",
    )
    dataset_sha256: str | None = param(
        DEFAULT_REAL_DATASET_SHA256,
        help="Expected SHA-256 for --dataset-url downloads. Empty disables checksum validation.",
        doc="Expected SHA-256 of the dataset zip; empty or None checks none.",
    )
    dataset_cache_dir: Path | None = param(
        None,
        help="Directory for cached real benchmark dataset downloads.",
        doc="Folder for cached real dataset downloads (default: the dev "
        "download cache when enabled, else <outdir>/datasets).",
        path="directory",
    )


def benchmark(
    params: BenchmarkParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Run the benchmark (see ``BenchmarkParams``)."""
    from wgsextract_cli.commands.benchmark.runner import run_benchmark

    return execute(params, run_benchmark, progress=progress, cancel=cancel, check=check)


__all__ = ["BenchmarkParams", "benchmark"]
