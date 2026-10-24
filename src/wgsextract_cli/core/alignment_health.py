"""How an alignment was made, judged from a small indexed sample.

Whether duplicates were marked (WGS Extract v4 never marked them), how many
base-quality values the sequencer wrote (binned or full resolution), and
whether the mapping was ALT-aware (see alt_mapping). Callers count unmarked
PCR and optical copies as independent evidence, so they warn about that too.
"""

import logging
import subprocess
from typing import Any

from wgsextract_cli.core.alt_mapping import alt_unaware, alt_unaware_message
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.utils import run_command

# chr1 here has no gaps or ALT copies in GRCh37 or GRCh38, so a short-read 30x
# genome has ~40,000 reads in it and typically 1-25% are duplicates.
SAMPLE_START, SAMPLE_END = 20_000_000, 20_200_000
MIN_SAMPLE_READS = 2000
# Long reads (PacBio, ONT) aren't duplicate-marked as a rule.
LONG_READ_LENGTH = 1000
QUALITY_SAMPLE = 3000


def _sample_region(header: str) -> str | None:
    for line in header.splitlines():
        if not line.startswith("@SQ"):
            continue
        fields = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
        if fields.get("SN") in ("chr1", "1") and int(fields.get("LN", 0)) > SAMPLE_END:
            return f"{fields['SN']}:{SAMPLE_START}-{SAMPLE_END}"
    return None


def alignment_health(alignment: PathInput, ref: str | None = None) -> dict[str, Any]:
    """duplicates_marked, duplicate_pct, quality_levels and alt_aware, each None
    when the sample can't say (no index or chr1, too few reads, long reads)."""
    health: dict[str, Any] = dict.fromkeys(
        ("duplicates_marked", "duplicate_pct", "quality_levels", "alt_aware")
    )
    unaware = alt_unaware(alignment, ref)
    health["alt_aware"] = None if unaware is None else not unaware
    if not alignment_index(alignment):
        return health
    cram = ["-T", ref] if ref else []
    try:
        header = run_command(
            ["samtools", "view", "-H", *cram, str(alignment)], capture_output=True
        ).stdout
        region = _sample_region(header)
        if not region:
            return health
        reads = run_command(
            ["samtools", "view", "-F", "0x900", *cram, str(alignment), region],
            capture_output=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Could not sample {alignment}: {e}")
        return health
    fields = [r.split("\t", 11) for r in reads]
    sample = fields[:QUALITY_SAMPLE]
    if sample:
        health["quality_levels"] = len({c for f in sample for c in f[10]} - {"*"})
    lengths = sorted(len(f[9]) for f in sample)
    if len(fields) < MIN_SAMPLE_READS or lengths[len(lengths) // 2] >= LONG_READ_LENGTH:
        return health
    duplicates = sum(int(f[1]) & 0x400 != 0 for f in fields)
    health["duplicates_marked"] = duplicates > 0
    health["duplicate_pct"] = round(100 * duplicates / len(fields), 1)
    return health


def duplicates_unmarked_message(alignment: PathInput) -> str:
    return (
        f"No read in a sample of {alignment} is marked as a duplicate, so "
        "duplicates were probably never marked (WGS Extract v4 didn't mark "
        "them): PCR and optical copies of a fragment count as independent "
        "evidence, inflating depth and confidence. `wgsextract realign` marks them."
    )


def warn_about_alignment(alignment: PathInput, ref: str | None = None) -> None:
    """Warn about an input alignment made in a way that loses or inflates calls:
    ALT-unaware mapping, or duplicates never marked."""
    health = alignment_health(alignment, ref)
    if health["alt_aware"] is False:
        logging.warning(alt_unaware_message(alignment))
    if health["duplicates_marked"] is False:
        logging.warning(duplicates_unmarked_message(alignment))
