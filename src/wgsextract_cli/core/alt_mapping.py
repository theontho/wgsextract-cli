"""Detect alignments to a reference with ALT contigs that weren't ALT-aware.

GRCh38's full set (hs38DH and the like) adds 261 ALT haplotype contigs, ~110 Mb
repeating stretches of the primary chromosomes. An aligner that doesn't know
which contigs are ALTs finds each read there twice: it gets mapping quality 0
on the chromosome, or lands on the ALT copy, and variant callers lose those
regions (HLA among them). WGS Extract v4 aligned to hs38DH this way.
"""

import logging
import subprocess

from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.utils import run_command

# chr1_KI270762v1_alt is placed at chr1:2,448,811-2,791,270; in this window it
# matches the chromosome closely enough that every read is ambiguous without
# ALT-aware mapping, while ALT-aware alignments give the reads normal MAPQ.
PROBE_ALT_CONTIG = "chr1_KI270762v1_alt"
PROBE_REGION = "chr1:2470000-2480000"
MIN_PROBE_READS = 200
UNAWARE_MAPQ0_FRACTION = 0.9


def alt_unaware(alignment: PathInput, ref: str | None = None) -> bool | None:
    """Whether the alignment has ALT contigs but wasn't mapped ALT-aware;
    None when it can't tell (no ALT contigs, too few reads, unreadable)."""
    try:
        header = run_command(
            ["samtools", "view", "-H", str(alignment)], capture_output=True
        ).stdout
        if f"SN:{PROBE_ALT_CONTIG}\t" not in header:
            return None
        reads = run_command(
            ["samtools", "view", "-F", "0x904"]
            + (["-T", ref] if ref else [])
            + [str(alignment), PROBE_REGION],
            capture_output=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Could not check ALT-aware mapping of {alignment}: {e}")
        return None
    if len(reads) < MIN_PROBE_READS:
        return None
    mapq0 = sum(r.split("\t", 5)[4] == "0" for r in reads)
    return mapq0 >= UNAWARE_MAPQ0_FRACTION * len(reads)


def alt_unaware_message(alignment: PathInput) -> str:
    return (
        f"{alignment} was aligned to a reference with ALT contigs without "
        "ALT-aware mapping: reads in the ~110 Mb that ALT haplotypes repeat "
        "(HLA among them) have mapping quality 0 or sit on the ALT copy, so "
        "their variants will be missing. Re-align with `wgsextract align`, "
        "which maps ALT-aware, for calls there."
    )


def warn_if_alt_unaware(alignment: PathInput, ref: str | None = None) -> None:
    if alt_unaware(alignment, ref):
        logging.warning(alt_unaware_message(alignment))
