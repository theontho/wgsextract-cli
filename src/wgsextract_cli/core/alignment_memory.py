"""Memory budgets for the concurrent native BWA/MEM2 alignment pipeline.

Full 30x H2 measured a 17–17.5 GiB BWA working set including paged-out memory,
not just its capped resident peak. Budget index files, 2 GiB aligner working
memory, roughly 6 GiB samblaster signatures, sort plus 25%, and bookkeeping
with input-read-scaled transient headroom: about 18 GiB for BWA or 29 GiB
for MEM2 at six threads/256M and 680 million reads. These are estimates,
not an allocation or a hard limit.
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
_ALIGNER_WORKING_BYTES = 2 * _GIB
_H2_INPUT_READS = 679_639_896
# H2 samblaster peaked at 17.7 bytes/pair; allow 19 (9.5/individual read).
_SAMBLASTER_BYTES_PER_PAIR = 19
_PIPELINE_OVERHEAD_BYTES = _GIB // 2
# Full H2 RSS/anonymous+swap observations require headroom beyond steady stages.
_H2_TRANSIENT_BYTES = 5 * _GIB // 2


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
    reads: int | None = None,
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
    input_reads = max(0, reads) if reads is not None else _H2_INPUT_READS
    return MappingMemory(
        index,
        _ALIGNER_WORKING_BYTES,
        (input_reads * _SAMBLASTER_BYTES_PER_PAIR + 1) // 2 if samblaster else 0,
        int(int(threads) * per_thread * 1.25),
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
) -> None:
    estimate = estimate_mapping_memory(
        ref, aligner, threads=threads, memory=memory, samblaster=samblaster, reads=reads
    )
    available = available_memory()
    components = ", ".join(
        f"{name.replace('_', ' ')} {value / _GIB:.1f}"
        for name, value in zip(estimate._fields, estimate, strict=True)
    )
    logging.info(
        "%s alignment memory estimate: %.2f GiB for %s input reads (%s GiB).",
        aligner,
        estimate.total / _GIB,
        f"{reads:,}" if reads is not None else "unknown; 680-million-read fallback",
        components,
    )
    if estimate.total <= available:
        return
    message = (
        f"{aligner} alignment is estimated to need {estimate.total / _GIB:.2f} GiB "
        f"but only {available / _GIB:.2f} GiB RAM is available "
        f"({components} GiB). Lower --memory (per sort thread) or --threads, "
        "or free memory."
    )
    if reads is None:
        message += (
            " No reliable input read count was available; using a 680-million-read "
            "fallback. Larger inputs may need more."
        )
    if aligner == "bwa-mem2":
        raise WGSExtractError(message + " Use --aligner bwa for its smaller index.")
    logging.warning("%s Continuing with bwa.", message)
