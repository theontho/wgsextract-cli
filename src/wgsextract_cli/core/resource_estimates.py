"""Read-only, GUI-reusable resource estimates; all sizes are bytes."""

import logging
import math
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import alignment_memory
from .native_support import is_linux_arm64
from .reference_assets import read_fai_lengths
from .region_selection import selected_intervals
from .samtools_commands import parse_memory

Operation = Literal[
    "align",
    "realign",
    "sort",
    "to-bam",
    "to-cram",
    "snp",
    "indel",
    "gatk",
    "deepvariant",
]
GIB = 1024**3
GATK_WORKER_BYTES = 6 * GIB
# Conservative 30x envelope: 680M reads -> 81.6 GB sort spill, 19.0 GB CRAM.
# H2's binned-quality CRAM was only 8.63 GB; don't assume all inputs compress so well.
SORT_BYTES_PER_READ = 120
CRAM_BYTES_PER_READ = 28
BAM_BYTES_PER_READ = 100
FASTQ_BYTES_PER_READ = 75
# DeepVariant postprocess_variants: each partition worker loads and sorts its
# partition's call_variants records in memory. #173 (HG008 Ultima ~30x, hs38DH,
# --cpus 5): DeepVariant's five partitions held 1.06-3.30 million records; the
# densest, chr13-chr20 (700,430,089 bases), held 3,295,319 (4.70 per kb). Three
# workers were OOM-killed mid-sort at 3.39-6.14 GB anonymous RSS, below their peaks.
DV_RECORDS_PER_BASE = 4.75e-3
DV_BYTES_PER_RECORD = 2048
DV_WORKER_BASE_BYTES = 768 * 1024**2
DV_MAX_PARTITIONS = 128


@dataclass(frozen=True)
class ResourceEstimate:
    resource: str
    estimated_bytes: int | None
    available_bytes: int | None
    components: dict[str, int]
    paths: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.estimated_bytes is None or self.available_bytes is None:
            return "UNKNOWN"
        return "OK" if self.estimated_bytes <= self.available_bytes else "LOW"

    def to_dict(self) -> dict[str, object]:
        return {
            "resource": self.resource,
            "estimated_bytes": self.estimated_bytes,
            "available_bytes": self.available_bytes,
            "status": self.status,
            "components": dict(self.components),
            "paths": list(self.paths),
        }


@dataclass(frozen=True)
class PreflightEstimate:
    operation: Operation
    resources: tuple[ResourceEstimate, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Versioned JSON-compatible data, without executing any external tool."""
        return {
            "schema_version": 1,
            "operation": self.operation,
            "resources": [resource.to_dict() for resource in self.resources],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class PostprocessPlan:
    """DeepVariant postprocess_variants workers (--cpus) and --num_partitions."""

    cpus: int
    partitions: int
    worker_bytes: int
    fits: bool | None

    @property
    def peak_bytes(self) -> int:
        return self.cpus * self.worker_bytes


def dv_calling_regions(
    lengths: dict[str, int], region: str | None
) -> list[tuple[str, int, int]] | None:
    """DeepVariant's calling regions in reference order; None without lengths."""
    if not lengths:
        return None
    if not region:
        return [(contig, 0, length) for contig, length in lengths.items()]
    selected = selected_intervals(region, lengths)
    return [(c, start, end) for c in lengths for start, end in selected.get(c, ())]


def dv_partition_bases(
    regions: Sequence[tuple[str, int, int]], partitions: int
) -> list[int] | None:
    """Bases per partition from DeepVariant 1.9+'s partition_calling_regions.

    None when DeepVariant itself would fail: fewer bases than partitions, or a
    single oversized piece left to split.
    """
    total = sum(end - start for _, start, end in regions)
    size = total // partitions
    if size < 1:
        return None
    pieces = [
        min(end, at + size) - at
        for _, start, end in regions
        for at in range(start, end, size)
    ]
    prefix = [0]
    for piece in pieces:
        prefix.append(prefix[-1] + piece)

    def bases(group: tuple[int, int]) -> int:
        return prefix[group[1]] - prefix[group[0]]

    # Groups are contiguous [first, last) piece ranges, closed once they exceed size.
    groups: list[tuple[int, int]] = []
    first = 0
    for index in range(len(pieces)):
        if index > first and prefix[index] - prefix[first] > size:
            groups.append((first, index))
            first = index
    groups.append((first, len(pieces)))
    while len(groups) < partitions:
        groups.sort(key=bases)
        first, last = groups.pop()
        middle = first + (last - first) // 2
        if middle == first:
            return None
        groups += [(first, middle), (middle, last)]
    return [bases(group) for group in sorted(groups)]


def dv_postprocess_worker_bytes(bases: int) -> int:
    records = math.ceil(bases * DV_RECORDS_PER_BASE)
    return DV_WORKER_BASE_BYTES + records * DV_BYTES_PER_RECORD


def plan_dv_postprocess(
    regions: Sequence[tuple[str, int, int]],
    threads: int,
    available: int | None,
    partitions: bool = True,
) -> PostprocessPlan:
    """Bound postprocess_variants' in-memory partition sorts by available RAM.

    DeepVariant's default is ``threads`` workers over as many partitions. If
    that does not fit beside 1 GiB of driver headroom, keep the most workers
    and add the fewest partitions that make their largest partitions fit.
    Without known RAM the default is kept (``fits`` None); when one worker
    cannot fit, the plan is the smallest one-worker plan with ``fits`` False.
    Without ``partitions`` (DeepVariant before 1.9 has no --num_partitions),
    only the workers, and so the partitions with them, are reduced.
    """
    workers: dict[int, int | None] = {}

    def worker(partitions: int) -> int | None:
        if partitions not in workers:
            sizes = dv_partition_bases(regions, partitions)
            workers[partitions] = (
                None if sizes is None else dv_postprocess_worker_bytes(max(sizes))
            )
        return workers[partitions]

    default = worker(threads)
    if default is None:
        # DeepVariant runs (or fails) as it would have; keep its default.
        total = sum(end - start for _, start, end in regions)
        return PostprocessPlan(
            threads, threads, dv_postprocess_worker_bytes(total), None
        )
    if available is None:
        return PostprocessPlan(threads, threads, default, None)
    budget = available - GIB
    limit = max(DV_MAX_PARTITIONS, threads)
    for cpus in range(threads, 0, -1):
        for count in range(cpus, (limit if partitions else cpus) + 1):
            need = worker(count)
            if need is not None and cpus * need <= budget:
                return PostprocessPlan(cpus, count, need, True)
    smallest = min(
        range(1, (limit if partitions else 1) + 1),
        key=lambda count: worker(count) or math.inf,
    )
    return PostprocessPlan(1, smallest, worker(smallest) or default, False)


def _disk_space(path: str) -> tuple[object, int | None]:
    existing = Path(path).expanduser().resolve()
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        return existing.stat().st_dev, shutil.disk_usage(existing).free
    except OSError:
        return str(existing), None


def _input_size(paths: Sequence[str]) -> int | None:
    try:
        if not paths or any(not Path(path).is_file() for path in paths):
            return None
        return sum(Path(path).stat().st_size for path in dict.fromkeys(paths))
    except OSError:
        return None


def estimate_resources(
    operation: Operation,
    *,
    input_paths: Sequence[str],
    output_dir: str,
    reference: str | None = None,
    threads: str = "1",
    memory: str = "512M",
    aligner: str = "bwa",
    reads: int | None = None,
    platform: str | None = None,
    read_length: float | None = None,
    output_format: str = "BAM",
    samblaster: bool = True,
    extract_fastq: bool = False,
    fastq_dir: str | None = None,
    temp_dir: str | None = None,
    gatk_staging_dir: str | None = None,
    region: str | None = None,
    duplicate_marking: Literal["samblaster", "markdup", "gatk-flow"] | None = None,
    parallel: bool = False,
    gatk_workers: int = 1,
    additional_ram_bytes: int = 0,
    inference_batch_size: int = 1024,
    deepvariant_model: str = "WGS",
    postprocess_plan: PostprocessPlan | None = None,
    available_ram: int | None = None,
) -> PreflightEstimate:
    """Estimate a resolved command without creating files or running tools.

    ``reads`` counts individual reads, not pairs. Callers may provide a cheap
    FASTQ sample/indexed count. Otherwise disk uses a conservative byte-size
    fallback; alignment RAM retains the D3 model's explicit unknown-count budget.
    Regional operations retain whole-input disk bounds, not guessed coverage.
    Callers that already planned DeepVariant's postprocess_variants pass that
    ``postprocess_plan`` and the ``available_ram`` it was sized against (for
    example capped by Docker's VM); otherwise both are derived here.
    """
    count = int(threads)
    if (
        count < 1
        or gatk_workers < 1
        or (reads is not None and reads < 0)
        or additional_ram_bytes < 0
        or inference_batch_size < 1
    ):
        raise ValueError(
            "threads/workers/batch size must be positive and reads/additional RAM nonnegative"
        )
    amount, unit = parse_memory(memory)
    sort_memory = math.ceil(count * amount * (GIB if unit == "G" else 1024**2) * 1.25)
    size = _input_size(input_paths)
    cram_input = bool(input_paths) and input_paths[0].lower().endswith(".cram")
    bam_input = bool(input_paths) and input_paths[0].lower().endswith(".bam")
    notes = [
        "Conservative estimates, not allocations or hard limits; existing inputs are excluded."
    ]
    if region:
        notes.append("Regional disk estimates use the whole input as an upper bound.")
    disk_reads = reads
    if disk_reads is None and size is not None:
        bytes_per_read = 12 if cram_input else (80 if bam_input else 70)
        disk_reads = math.ceil(size / bytes_per_read)
        notes.append(
            "Disk read count inferred from input bytes; compression and read lengths vary."
        )
    available_memory: int | None = available_ram
    if available_memory is None:
        try:
            available_memory = alignment_memory.available_memory()
        except OSError:
            available_memory = None
    demands: list[tuple[str, str, int | None]] = []
    memory_components: dict[str, int]
    output: int | None
    temporary: int | None = 0
    if operation in {"align", "realign"}:
        if not reference:
            notes.append(
                "Reference unresolved: RAM uses the human-reference fallback; reference-index disk is unknown."
            )
        mapping = alignment_memory.estimate_mapping_memory(
            reference,
            aligner,
            threads=threads,
            memory=memory,
            samblaster=samblaster and duplicate_marking != "gatk-flow",
            reads=reads,
            platform=platform,
            read_length=read_length,
        )
        if aligner == "bwa":
            length = (
                read_length
                if read_length is not None
                else (300 if platform == "ultima" else 150)
            )
            notes.append(
                f"BWA working memory uses {platform or 'short-read'} / {length:g} bp "
                f"({'sampled/provided' if read_length is not None else 'fallback'}), "
                "requested threads and fixed -K 100000000; threads are not auto-capped."
            )
        memory_components = dict(zip(mapping._fields, mapping, strict=True))
        if additional_ram_bytes:
            memory_components["collation"] = additional_ram_bytes
            notes.append("Streamed FASTQ collation overlaps the alignment pipeline.")
            demands.append(
                (
                    temp_dir or output_dir,
                    "collation_spill",
                    None if disk_reads is None else disk_reads * SORT_BYTES_PER_READ,
                )
            )
        if reads is None:
            notes.append(
                "Alignment RAM uses the D3 680-million-read unknown-count fallback."
            )
        output = (
            None
            if disk_reads is None
            else disk_reads
            * (CRAM_BYTES_PER_READ if output_format == "CRAM" else BAM_BYTES_PER_READ)
        )
        temporary = None if disk_reads is None else disk_reads * SORT_BYTES_PER_READ
        if duplicate_marking == "gatk-flow":
            if disk_reads is not None:
                retained = (
                    disk_reads
                    * BAM_BYTES_PER_READ
                    * (2 if output_format == "CRAM" else 1)
                )
                temporary = max(temporary or 0, retained)
            notes.append(
                "GATK flow duplicate marking includes retained sorted/marked BAMs."
            )
        if extract_fastq:
            demands.append(
                (
                    fastq_dir or output_dir,
                    "extracted_fastqs",
                    None if disk_reads is None else disk_reads * FASTQ_BYTES_PER_READ,
                )
            )
        if reference:
            suffixes = (
                (".bwt", ".sa", ".pac", ".ann", ".amb")
                if aligner == "bwa"
                else (".0123", ".bwt.2bit.64", ".pac", ".ann", ".amb")
            )
            if not all(Path(reference + suffix).is_file() for suffix in suffixes):
                demands.append(
                    (
                        str(Path(reference).parent),
                        "reference_indexes",
                        mapping.index_resident * 2,
                    )
                )
                notes.append(
                    "Missing reference indexes include an additional build-space allowance."
                )
    elif operation == "sort":
        memory_components = {"sort": sort_memory, "overhead": GIB // 4}
        output = (
            None
            if disk_reads is None
            else disk_reads
            * (CRAM_BYTES_PER_READ if output_format == "CRAM" else BAM_BYTES_PER_READ)
        )
        temporary = None if disk_reads is None else disk_reads * SORT_BYTES_PER_READ
    elif operation in {"to-bam", "to-cram"}:
        memory_components = {"codecs": (count + 1) * 64 * 1024**2, "overhead": GIB // 4}
        output = (
            None
            if disk_reads is None
            else disk_reads
            * (CRAM_BYTES_PER_READ if operation == "to-cram" else BAM_BYTES_PER_READ)
        )
    elif operation == "deepvariant":
        arm64 = is_linux_arm64()
        inference = 2 * GIB + math.ceil(
            (30 if arm64 else 14) * GIB * inference_batch_size / 1024
        )
        sharded = count * (2 if arm64 else 1) * GIB
        # run_deepvariant postprocesses WES in one sequential pass (--cpus 0).
        regions = (
            None
            if deepvariant_model == "WES"
            else dv_calling_regions(
                read_fai_lengths(reference) if reference else {}, region
            )
        )
        plan = postprocess_plan or (
            None
            if regions is None
            else plan_dv_postprocess(regions, count, available_memory)
        )
        memory_components = {
            "largest_sequential_stage": max(
                inference, sharded, plan.peak_bytes if plan else 0
            ),
            "driver_headroom": GIB,
        }
        notes.append(
            "DeepVariant WGS uses sequential stages: allow for the largest of "
            "TensorFlow inference, concurrent make_examples shards and "
            "postprocess_variants partition sorts, not their sum. "
            "Reducing --threads does not remove the default inference memory floor; "
            "other models/checkpoints may require more RAM."
        )
        if regions is None and deepvariant_model != "WES":
            notes.append(
                "postprocess_variants RAM is unknown without a reference .fai."
            )
        elif plan is not None:
            notes.append(
                f"postprocess_variants: {plan.cpus} workers over {plan.partitions} "
                f"partitions, up to {plan.worker_bytes / GIB:.2f} GiB each "
                "(calibrated on 30x Ultima call density)."
            )
        output = None if size is None else math.ceil(size / 10)
        temporary = size
        notes.append(
            "DeepVariant temporary disk allows one input-sized pileup/intermediate "
            "working set; actual coverage and candidate density can change it."
        )
    else:
        if operation == "gatk":
            memory_components = {"caller": gatk_workers * GATK_WORKER_BYTES}
            notes.append(
                "GATK RAM is a heuristic; Java heap and flow-mode settings can change its peak."
            )
            if cram_input and region:
                demands.append(
                    (
                        gatk_staging_dir or output_dir,
                        "regional_bam",
                        None if disk_reads is None else disk_reads * BAM_BYTES_PER_READ,
                    )
                )
        else:
            contigs = read_fai_lengths(reference) if reference else {}
            workers = 1 if region or not parallel else min(count, len(contigs) or count)
            memory_components = {"callers": workers * GIB, "overhead": GIB // 4}
            notes.append(
                "bcftools RAM allows 1 GiB per active contig worker plus overhead."
            )
        output = None if size is None else math.ceil(size / 10)
        temporary = (
            None if output is None else output * (2 if operation == "gatk" else 1)
        )
    indexes = None if output is None else max(64 * 1024, math.ceil(output / 100))
    if operation == "gatk" and gatk_workers > 1:
        # Shards coexist with the gathered output.
        demands += [
            (gatk_staging_dir or output_dir, "scatter_parts", output),
            (gatk_staging_dir or output_dir, "scatter_indexes", indexes),
        ]
    demands += [
        (output_dir, "output", output),
        (output_dir, "indexes", indexes),
        (temp_dir or output_dir, "temporary", temporary),
    ]
    grouped: dict[object, tuple[int | None, dict[str, int], list[str], bool]] = {}
    for path, component, needed in demands:
        if needed == 0:
            continue
        device, available = _disk_space(path)
        if device not in grouped:
            grouped[device] = (available, {}, [], False)
        old_available, components, paths, unknown = grouped[device]
        if path not in paths:
            paths.append(path)
        if needed is not None:
            components[component] = components.get(component, 0) + needed
        free = (
            min(old_available, available)
            if old_available is not None and available is not None
            else None
        )
        grouped[device] = (free, components, paths, unknown or needed is None)
    resources = [
        ResourceEstimate(
            "memory",
            sum(memory_components.values()),
            available_memory,
            memory_components,
        )
    ]
    resources += [
        ResourceEstimate(
            "disk",
            None if unknown else sum(components.values()),
            available,
            components,
            tuple(paths),
        )
        for available, components, paths, unknown in grouped.values()
    ]
    if operation in {"align", "realign"} and not reference:
        resources.append(ResourceEstimate("disk", None, None, {}))
    return PreflightEstimate(operation, tuple(resources), tuple(notes))


def preflight_resources(estimate: Callable[[], PreflightEstimate]) -> None:
    """Best-effort CLI reporting; the GUI estimator remains strict."""
    try:
        report_resources(estimate())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        logging.warning("resource estimate unavailable: %s", exc)


def report_resources(estimate: PreflightEstimate) -> None:
    """Log one compact row per resource/filesystem, warning on shortages."""
    logging.info("Preflight %s: estimated peak / available (GiB)", estimate.operation)
    for resource in estimate.resources:
        needed = (
            "unknown"
            if resource.estimated_bytes is None
            else f"{resource.estimated_bytes / GIB:.2f}"
        )
        available = (
            "unknown"
            if resource.available_bytes is None
            else f"{resource.available_bytes / GIB:.2f}"
        )
        paths = f" at {', '.join(resource.paths)}" if resource.paths else ""
        logging.log(
            logging.INFO if resource.status == "OK" else logging.WARNING,
            "Preflight %-6s %-7s %s / %s GiB%s",
            resource.resource,
            resource.status,
            needed,
            available,
            paths,
        )
    for note in estimate.notes:
        logging.info("Preflight: %s", note)
