"""Memory budgets for the concurrent native BWA/MEM2 and minimap2 alignment
pipelines.

Full 30x H2 measured a 17–17.5 GiB BWA working set including paged-out memory,
not just its capped resident peak. Budget index files, aligner working
memory, roughly 6 GiB samblaster signatures, sort plus 25%, and bookkeeping
with input-read-scaled transient headroom: about 18 GiB for BWA or 29 GiB
for MEM2 at six threads/256M and 680 million reads. Ultima BWA working memory
uses the HG008 300-bp, fixed-K100M thread-dependent envelope instead of the
ordinary short-read 2 GiB allowance. These are estimates, not an allocation
or a hard limit.
"""

import logging
import math
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import psutil

from wgsextract_cli.core.paths import append_suffix
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.results import warn
from wgsextract_cli.core.samtools_commands import parse_memory
from wgsextract_cli.core.utils import WGSExtractError

_GIB = 1024**3
_HUMAN_REFERENCE_BASES = 3_217_346_917
_INDEX_PARTS = {
    "bwa": ((".bwt", ".sa", ".pac"), 1.75),
    "bwa-mem2": ((".0123", ".bwt.2bit.64", ".pac"), 5.5),
}
_ALIGNER_WORKING_BYTES = 2 * _GIB
_H2_INPUT_READS = 679_639_896
# H2 samblaster peaked at 17.7 bytes/pair; allow 19 (9.5/individual read).
_SAMBLASTER_BYTES_PER_PAIR = 19
_PIPELINE_OVERHEAD_BYTES = _GIB // 2
# Full H2 RSS/anonymous+swap observations require headroom beyond steady stages.
_H2_TRANSIENT_BYTES = 5 * _GIB // 2
_BWA_BATCH_BASES = 100_000_000
# minimap2 2.31 builds its index from the FASTA on every run. On hs38 (3.10
# Gbases) the build peaked at 11.1-11.3 GiB with every preset but map-pb's
# homopolymer-compressed 8.6 GiB. The built index (as a saved .mmi) is smaller:
# the build's scratch is freed before mapping starts.
_MINIMAP2_BUILD_BYTES_PER_BASE = {"map-pb": 3.05}
_MINIMAP2_BUILD_DEFAULT = 3.95
_MINIMAP2_INDEX_BYTES_PER_BASE = {
    "map-ont": 2.35,
    "map-hifi": 1.86,
    "lr:hq": 1.86,
    "map-pb": 1.82,
    "sr": 2.80,
}
# Mapping holds batches of -K 500M bases (read, mapped, written) beside the
# index, plus each thread's alignment buffers, which grow with read length:
# 40k HG002 ONT reads (20 kb mean) at -t 4 peaked 4.2 GiB above the index.
_MINIMAP2_BATCH_BYTES = 5 * _GIB // 2
# That is about 5.4 bytes per batched base. Even reference-compressed CRAM with
# binned qualities holds fewer than 16 bases per byte, so read files smaller
# than about 30 MB fill one smaller batch. The same bound caps samtools sort's
# buffers and the threads' work, which only grow as reads arrive.
_DECODED_BYTES_PER_INPUT_BYTE = 96
# gzip/BGZF stores a FASTA's bases in at least 0.2 bytes each.
_FASTA_GZ_BASES_PER_BYTE = 5
_MINIMAP2_THREAD_BYTES = _GIB // 2


class MappingMemory(NamedTuple):
    index_resident: int
    working: int
    samblaster: int
    sort: int
    overhead: int

    @property
    def total(self) -> int:
        return sum(self)


def available_memory() -> int:
    """Reclaimable physical memory, using psutil's native API on Windows too."""
    available = max(0, int(psutil.virtual_memory().available))
    capped = _cgroup_available_memory()
    return min(available, capped) if capped is not None else available


def _cgroup_available_memory(
    membership: Path = Path("/proc/self/cgroup"),
    root: Path = Path("/sys/fs/cgroup"),
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> int | None:
    """Honor Linux cgroup-v2 ancestor caps, not just the host's free RAM."""
    try:
        relative = next(
            line[3:]
            for line in membership.read_text().splitlines()
            if line.startswith("0::")
        )
        path = PurePosixPath(relative)
        if ".." in path.parts:
            group = root
        else:
            mounted = _cgroup_mount_root(root, mountinfo)
            try:
                path = path.relative_to(mounted)
            except ValueError:
                # Membership may already be relative to a cgroup namespace.
                path = PurePosixPath(relative.lstrip("/"))
            group = root / str(path)
        # A container can expose only the namespace root rather than the
        # host-relative membership path.
        if not group.is_dir():
            group = root
        budgets = []
        while True:
            try:
                limit = (group / "memory.max").read_text().strip()
                if limit != "max":
                    current = int((group / "memory.current").read_text())
                    reclaimable = 0
                    try:
                        for line in (group / "memory.stat").read_text().splitlines():
                            name, value = line.split()
                            if name == "inactive_file":
                                reclaimable = int(value)
                    except (OSError, ValueError):
                        pass
                    budgets.append(
                        min(int(limit), max(0, int(limit) - current + reclaimable))
                    )
            except (OSError, ValueError):
                pass
            if group == root:
                break
            group = group.parent
        return min(budgets) if budgets else None
    except (OSError, StopIteration):
        return None


def _cgroup_mount_root(root: Path, mountinfo: Path) -> PurePosixPath:
    """Translate the visible cgroup2 mount's filesystem-root offset."""

    def decoded(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    try:
        for line in mountinfo.read_text().splitlines():
            fields = line.split()
            try:
                separator = fields.index("-")
                if (
                    fields[separator + 1] == "cgroup2"
                    and Path(decoded(fields[4])) == root
                ):
                    return PurePosixPath(decoded(fields[3]))
            except (ValueError, IndexError):
                continue
    except OSError:
        pass
    return PurePosixPath("/")


def _working_memory(
    aligner: str,
    threads: int,
    reads: int | None,
    platform: str | None,
    read_length: float | None,
) -> int:
    if read_length is not None and (not math.isfinite(read_length) or read_length <= 0):
        raise ValueError("read_length must be finite and positive")
    if threads < 1:
        raise ValueError("threads must be positive")
    if aligner != "bwa":
        return _ALIGNER_WORKING_BYTES
    if platform == "ultima":
        length = read_length if read_length is not None else 300
        batch_fraction = (
            min(1, reads * length / _BWA_BATCH_BASES) if reads is not None else 1
        )
        # HG008, hs38DH, -K 100M: BWA alone reached 9.18 GiB at t2 and
        # 14.70 GiB anonymous memory at t4, including its ~5.2 GiB index.
        # Allow 4.5/10 GiB working memory at 300 bp; longer reads cost more.
        working = (
            (threads * 11 * _GIB / 4 - _GIB) * max(0.5, length / 300) * batch_fraction
        )
    else:
        length = read_length if read_length is not None else 150
        working = _ALIGNER_WORKING_BYTES * max(1, length / 150) * max(1, threads / 6)
    return math.ceil(max(_ALIGNER_WORKING_BYTES, working))


def _minimap2_memory(
    bases: int, preset: str | None, threads: int, input_bytes: int | None
) -> tuple[int, int]:
    """(resident index, working memory beyond it) for minimap2. An unknown
    preset gets the largest index. The build's scratch counts as working
    memory while it exceeds mapping's."""
    if threads < 1:
        raise ValueError("threads must be positive")
    index = math.ceil(
        bases
        * _MINIMAP2_INDEX_BYTES_PER_BASE.get(
            preset or "", max(_MINIMAP2_INDEX_BYTES_PER_BASE.values())
        )
    )
    build = math.ceil(
        bases
        * _MINIMAP2_BUILD_BYTES_PER_BASE.get(preset or "", _MINIMAP2_BUILD_DEFAULT)
    )
    batch = _MINIMAP2_BATCH_BYTES
    thread_work = threads * _MINIMAP2_THREAD_BYTES
    if input_bytes is not None:
        decoded = max(0, input_bytes) * _DECODED_BYTES_PER_INPUT_BYTE
        batch = min(batch, decoded)
        # One long read can still need a whole thread's alignment buffers.
        thread_work = min(thread_work, max(_MINIMAP2_THREAD_BYTES, decoded))
    mapping = batch + thread_work
    return index, max(build - index, mapping)


def _fasta_bases_bound(ref: str | None) -> int:
    """At most how many bases a FASTA without a .fai holds, from its size."""
    try:
        size = Path(ref).stat().st_size if ref else 0
    except OSError:
        return 0
    gz = str(ref).lower().endswith((".gz", ".bgz", ".bgzf"))
    return size * _FASTA_GZ_BASES_PER_BYTE if gz else size


def reference_bases(ref: str | None) -> int:
    if not ref:
        return 0
    lengths = read_fai_lengths(ref)
    if lengths:
        return sum(lengths.values())
    # BWA indexes already carry the length, even before a FASTA .fai exists.
    try:
        with append_suffix(ref, ".ann").open() as ann:
            return max(0, int(ann.readline().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def estimate_mapping_memory(
    ref: str | None,
    aligner: str,
    *,
    threads: str = "1",
    memory: str = "512M",
    samblaster: bool = True,
    reads: int | None = None,
    platform: str | None = None,
    read_length: float | None = None,
    preset: str | None = None,
    input_bytes: int | None = None,
) -> MappingMemory:
    """Peak memory of the alignment pipeline. ``preset`` is minimap2's, and
    ``input_bytes`` (the read files' size) bounds its batches."""
    bases = (
        reference_bases(ref)
        or (_fasta_bases_bound(ref) if aligner == "minimap2" else 0)
        or _HUMAN_REFERENCE_BASES
    )
    amount, unit = parse_memory(memory)
    per_thread = amount * (1024**3 if unit == "G" else 1024**2)
    sort = int(int(threads) * per_thread * 1.25)
    input_reads = max(0, reads) if reads is not None else _H2_INPUT_READS
    if aligner == "minimap2":
        index, working = _minimap2_memory(bases, preset, int(threads), input_bytes)
        if input_bytes is not None:
            # samtools sort fills its buffers only with the reads it receives.
            sort = min(sort, max(0, input_bytes) * _DECODED_BYTES_PER_INPUT_BYTE)
        if reads is None and input_bytes is not None:
            # A compressed short read takes at least 8 bytes.
            input_reads = min(input_reads, max(0, input_bytes) // 8)
        return MappingMemory(
            index,
            working,
            # Only short reads (preset sr) have their duplicates marked.
            (input_reads * _SAMBLASTER_BYTES_PER_PAIR + 1) // 2 if samblaster else 0,
            sort,
            _PIPELINE_OVERHEAD_BYTES,
        )
    suffixes, bytes_per_base = _INDEX_PARTS[aligner]
    # A missing/unknown index may be built later; don't omit it from the preflight.
    index = int(bases * bytes_per_base)
    if ref:
        try:
            index = sum(
                append_suffix(ref, suffix).stat().st_size for suffix in suffixes
            )
        except OSError:
            pass
    return MappingMemory(
        index,
        _working_memory(aligner, int(threads), reads, platform, read_length),
        (input_reads * _SAMBLASTER_BYTES_PER_PAIR + 1) // 2 if samblaster else 0,
        sort,
        _PIPELINE_OVERHEAD_BYTES + input_reads * _H2_TRANSIENT_BYTES // _H2_INPUT_READS,
    )


def check_mapping_memory(
    ref: str,
    aligner: str,
    *,
    threads: str,
    memory: str,
    samblaster: bool,
    reads: int | None = None,
    platform: str | None = None,
    read_length: float | None = None,
    additional_ram_bytes: int = 0,
    preset: str | None = None,
    input_bytes: int | None = None,
) -> None:
    """Log the alignment's memory estimate, and warn (or for bwa-mem2, raise)
    with settings that fit when it exceeds the available memory."""

    def estimate(threads: str, memory: str) -> MappingMemory:
        return estimate_mapping_memory(
            ref,
            aligner,
            threads=threads,
            memory=memory,
            samblaster=samblaster,
            reads=reads,
            platform=platform,
            read_length=read_length,
            preset=preset,
            input_bytes=input_bytes,
        )

    requested = estimate(threads, memory)
    available = available_memory()
    components = ", ".join(
        f"{name.replace('_', ' ')} {value / _GIB:.1f}"
        for name, value in zip(requested._fields, requested, strict=True)
    )
    if additional_ram_bytes:
        components += f", collation {additional_ram_bytes / _GIB:.1f}"
    total = requested.total + additional_ram_bytes
    # minimap2's estimate depends on the input's size only for samblaster.
    counted = aligner != "minimap2" or samblaster
    logging.info(
        "%s alignment memory estimate: %.2f GiB%s (%s GiB).",
        aligner,
        total / _GIB,
        (
            " for "
            + (
                f"{reads:,}"
                if reads is not None
                else "unknown; 680-million-read fallback"
            )
            + " input reads"
        )
        if counted
        else "",
        components,
    )
    if total <= available:
        return
    message = (
        f"{aligner} alignment is estimated to need {total / _GIB:.2f} GiB "
        f"but only {available / _GIB:.2f} GiB RAM is available "
        f"({components} GiB). Lower --memory (per sort thread) or --threads, "
        "or free memory."
    )
    if reads is None and counted:
        message += (
            " No reliable input read count was available; using a 680-million-read "
            "fallback. Larger inputs may need more."
        )
    if aligner == "bwa-mem2":
        raise WGSExtractError(message + " Use --aligner bwa for its smaller index.")
    if aligner == "minimap2":
        message += (
            f" minimap2 builds a {requested.index_resident / _GIB:.1f} GiB index of "
            "the reference however few threads it has."
        )
    fit = _fitting_settings(
        lambda t, m: estimate(t, m).total + additional_ram_bytes <= available,
        int(threads),
        memory,
        # BWA's advice keeps the per-sort-thread memory.
        lower_memory=aligner == "minimap2",
    )
    if fit:
        message += (
            f" This estimate fits with --threads {fit[0]} --memory {fit[1]}; "
            "requested settings are unchanged."
        )
    else:
        least = estimate(
            "1", _SMALLEST_SORT_MEMORY if aligner == "minimap2" else memory
        )
        message += (
            " Even one thread exceeds the available-memory estimate: it needs "
            f"{(least.total + additional_ram_bytes) / _GIB:.1f} GiB."
        )
    message += f" Continuing with {aligner}."
    if aligner != "minimap2":
        # Without a read count BWA's estimate assumes a 680-million-read
        # genome, even for tiny inputs, so it stays a log line.
        logging.warning(message)
        return
    warn(
        "alignment_memory",
        message,
        aligner=aligner,
        estimated_gib=round(total / _GIB, 2),
        available_gib=round(available / _GIB, 2),
        suggested_threads=fit[0] if fit else None,
        suggested_memory=fit[1] if fit else None,
    )


_SMALLEST_SORT_MEMORY = "256M"


def _fitting_settings(
    fits: Callable[[str, str], bool], threads: int, memory: str, *, lower_memory: bool
) -> tuple[int, str] | None:
    """The most threads, then the most per-thread sort memory (no more than
    requested, down to 256M when ``lower_memory``), that ``fits``."""
    amount, unit = parse_memory(memory)
    requested = amount * (1024 if unit == "G" else 1)
    memories = [memory]
    if lower_memory:
        memories += [
            f"{mib // 1024}G" if mib >= 1024 else f"{mib}M"
            for mib in (4096, 2048, 1024, 768, 512, 256)
            if mib < requested
        ]
    for candidate in range(threads, 0, -1):
        for option in memories:
            if fits(str(candidate), option):
                return candidate, option
    return None
