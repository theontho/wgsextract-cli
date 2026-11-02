"""Shared, bounded platform detection for reads and alignments."""

import gzip
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.constants import SEQUENCERS
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.ultima import read_groups
from wgsextract_cli.core.utils import WGSExtractError, run_command

_PLATFORMS = {
    "ONT": "ont",
    "PACBIO": "pacbio",
    "ULTIMA": "ultima",
    "ILLUMINA": "illumina",
    "DNBSEQ": "dnbseq",
    "BGISEQ": "dnbseq",
}


def platform_from_metadata(
    header: str = "", qname: str | None = None, sequencer: str | None = None
) -> str | None:
    """Read groups take precedence; mixed platforms must not select one model."""
    platforms = {
        group["PL"].upper() for group in read_groups(header) if group.get("PL")
    }
    if platforms:
        return _PLATFORMS.get(next(iter(platforms))) if len(platforms) == 1 else None
    names = [sequencer] if sequencer else []
    if qname:
        names += [
            name
            for name, pattern in SEQUENCERS.items()
            if name != "Unknown" and re.search(pattern, qname)
        ]
    for name in names:
        if not name:
            continue
        if name.startswith("Oxford Nanopore"):
            return "ont"
        if name.startswith("PacBio"):
            return "pacbio"
        if name.startswith("Ultima"):
            return "ultima"
        if name.startswith("MGI"):
            return "dnbseq"
        if name.startswith(("Illumina", "Solexa")):
            return "illumina"
    return None


def input_platform(
    reads: PathInput | None,
    reference: PathInput | None = None,
    *,
    strict: bool = False,
) -> str | None:
    """Cache per file revision, not just path (outputs can be replaced in place).

    With ``strict``, an undecodable CRAM read-name probe is an error rather than
    "unknown", so alignment never silently falls back to short-read defaults.
    """
    if reads is None:
        return None
    path = Path(reads)
    try:
        stat = path.stat()
    except OSError:
        return None
    return _input_platform(
        str(path.resolve()),
        str(reference) if reference else None,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        strict,
    )


@lru_cache(maxsize=128)
def _input_platform(
    path: str,
    reference: str | None,
    size: int,
    mtime: int,
    ctime: int,
    strict: bool = False,
) -> str | None:
    if path.lower().endswith((".bam", ".cram", ".sam")):
        header = get_bam_header(path, reference)
        if any(group.get("PL") for group in read_groups(header)):
            return platform_from_metadata(header)
        try:
            result = run_command(
                [
                    "samtools",
                    "head",
                    "-h",
                    "0",
                    "-n",
                    "1",
                    *(["--reference", reference] if reference else []),
                    path,
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr
                )
            qname = result.stdout.split("\t", 1)[0].strip()
        except (OSError, subprocess.SubprocessError) as error:
            if strict and path.lower().endswith(".cram"):
                raise WGSExtractError(
                    "Unable to probe CRAM read names for platform detection; "
                    "ONT cannot be excluded. Supply the original decoding FASTA "
                    "(--input-ref for realign); refusing to silently choose "
                    "paired short-read defaults."
                ) from error
            return None
        return platform_from_metadata(header, qname)
    if path.lower().endswith(
        (".fq", ".fastq", ".fq.gz", ".fastq.gz", ".fq.bgz", ".fastq.bgz")
    ):
        opener = gzip.open if path.lower().endswith((".gz", ".bgz")) else open
        try:
            with opener(path, "rt") as handle:
                line = handle.readline(1024 * 1024)
        except (OSError, EOFError, UnicodeDecodeError):
            return None
        if line.startswith("@") and (fields := line[1:].split()):
            return platform_from_metadata(qname=fields[0])
    return None
