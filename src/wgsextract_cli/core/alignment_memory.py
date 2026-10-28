"""Memory budgets for the concurrent native BWA/MEM2 alignment pipeline.

The H2 OOM component breakdown motivates 2 GiB of aligner working memory,
projected roughly 6 GiB of samblaster signatures for a 30x human genome, 25% sort
overhead, and 0.5 GiB for Python and pipeline bookkeeping. Higher coverage
can require more; these are estimates, not an allocation or a hard limit.
"""

import logging
from typing import NamedTuple

import psutil

from wgsextract_cli.core.paths import append_suffix
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.samtools_commands import parse_memory
from wgsextract_cli.core.utils import WGSExtractError

_GIB = 1024**3
_HUMAN_REFERENCE_BASES = 3_217_346_917
_INDEX_PARTS = {
    "bwa": ((".bwt", ".sa", ".pac"), 1.75),
    "bwa-mem2": ((".0123", ".bwt.2bit.64", ".pac"), 5.5),
}
# Provisional component allowances; finalize against H2 attempt 3's full peak.
_ALIGNER_WORKING_BYTES = 2 * _GIB
_SAMBLASTER_BYTES_PER_BASE = 2
_PIPELINE_OVERHEAD_BYTES = _GIB // 2


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
    return max(0, int(psutil.virtual_memory().available))


def reference_bases(ref: str) -> int:
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
    ref: str,
    aligner: str,
    *,
    threads: str = "1",
    memory: str = "512M",
    samblaster: bool = True,
) -> MappingMemory:
    bases = reference_bases(ref) or _HUMAN_REFERENCE_BASES
    suffixes, bytes_per_base = _INDEX_PARTS[aligner]
    try:
        index = sum(append_suffix(ref, suffix).stat().st_size for suffix in suffixes)
    except OSError:
        # A missing index may be built later; don't omit it from the preflight.
        index = int(bases * bytes_per_base)
    amount, unit = parse_memory(memory)
    per_thread = amount * (1024**3 if unit == "G" else 1024**2)
    return MappingMemory(
        index,
        _ALIGNER_WORKING_BYTES,
        bases * _SAMBLASTER_BYTES_PER_BASE if samblaster else 0,
        int(int(threads) * per_thread * 1.25),
        _PIPELINE_OVERHEAD_BYTES,
    )


def check_mapping_memory(
    ref: str,
    aligner: str,
    *,
    threads: str,
    memory: str,
    samblaster: bool,
) -> None:
    estimate = estimate_mapping_memory(
        ref, aligner, threads=threads, memory=memory, samblaster=samblaster
    )
    available = available_memory()
    if estimate.total <= available:
        return
    components = ", ".join(
        f"{name.replace('_', ' ')} {value / _GIB:.1f}"
        for name, value in zip(estimate._fields, estimate, strict=True)
    )
    message = (
        f"{aligner} alignment is estimated to need {estimate.total / _GIB:.1f} GiB "
        f"but only {available / _GIB:.1f} GiB RAM is available "
        f"({components} GiB). Lower --memory (per sort thread) or --threads, "
        "or free memory."
    )
    if samblaster:
        message += " The samblaster allowance assumes about 30x coverage; higher coverage needs more."
    if aligner == "bwa-mem2":
        raise WGSExtractError(message + " Use --aligner bwa for its smaller index.")
    logging.warning("%s Continuing with bwa.", message)
