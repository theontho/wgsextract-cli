"""Ultima Genomics (UG 100) flow-based reads.

Ultima reads are single-end, ~300 bp, and their errors are mostly homopolymer
lengths, not substitutions. Besides QUAL, each read carries the flow-space
probabilities callers use to model that: ``tp`` (per base, the homopolymer
length error QUAL refers to) and ``t0`` (per base, the probability a 1-mer is
really absent). Ultima's aligner writes both in the orientation of SEQ ("tp:
reference" in the read group), so they must be reversed whenever a read's
strand changes: on its way back to FASTQ, and again after realignment. GATK's
flow mode also needs the read group's flow order (``FO``).

Running this module reverses tp and t0 on the reverse-strand records of a SAM
stream (stdin to stdout), the job Picard's ATTRIBUTE(S)_TO_REVERSE does in
Ultima's and Broad's pipelines.
"""

import gzip
import logging
import subprocess
import sys
from dataclasses import dataclass
from os import fspath

from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.utils import run_command

PLATFORM = "ULTIMA"
# Ultima's flow order, cycled for the whole run (the FO of every UG 100 run).
DEFAULT_FLOW_ORDER = "TGCA"
# Per-read tags kept through realignment: flow probabilities (tp, t0), the
# trimming reason (tm, which duplicate marking reads), read quality (rq) and
# the adapter position (a3).
READ_TAGS = ("tp", "t0", "tm", "rq", "a3")
_REVERSED_TAGS = (b"tp:B:", b"t0:Z:")
_SNIFF_READS = 200


def read_groups(header: str) -> list[dict[str, str]]:
    """The fields of each @RG line in a SAM header."""
    groups = []
    for line in header.splitlines():
        if line.startswith("@RG\t"):
            fields = (f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
            groups.append(dict(fields))
    return groups


def is_ultima_header(header: str) -> bool:
    return any(rg.get("PL", "").upper() == PLATFORM for rg in read_groups(header))


def alignment_is_ultima(alignment: PathInput) -> bool:
    """Whether a BAM/CRAM's read groups say it came from an Ultima sequencer."""
    from wgsextract_cli.core.alignment_metadata import get_bam_header

    return is_ultima_header(get_bam_header(alignment))


def bcftools_options(alignment: PathInput) -> tuple[list[str], list[str] | None]:
    """(mpileup options, call prior options or None to keep the caller's) for
    an alignment. For Ultima reads: bcftools' Ultima profile (-X ultima: its
    base-quality, BAQ and homopolymer indel settings; put it before other
    options, which override it), and call's default prior rather than -P 0,
    which on GIAB HG008 chr20 (against 169x Illumina) let homopolymer-length
    errors through as indels: precision 0.13 against 0.29."""
    if not alignment_is_ultima(alignment):
        return [], None
    logging.info("Ultima reads: calling with bcftools' Ultima profile (-X ultima).")
    return ["-X", "ultima"], []


def has_flow_order(header: str) -> bool:
    return any(
        rg.get("PL", "").upper() == PLATFORM and rg.get("FO")
        for rg in read_groups(header)
    )


def reads_have_flow_tags(
    alignment: PathInput, reference: PathInput | None = None
) -> bool:
    """Whether the first reads of a BAM/CRAM carry tp, the flow tag callers need."""
    cram = ["--reference", fspath(reference)] if reference else []
    try:
        result = run_command(
            ["samtools", "head", "-h", "0", "-n", str(_SNIFF_READS), *cram]
            + [fspath(alignment)],
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "\ttp:B:" in result.stdout


def fastq_has_flow_tags(fastq: PathInput) -> bool:
    """Whether a FASTQ's first record carries tp in its comment (SAM tags, as
    `samtools fastq -T` writes them), for `bwa mem -C` to copy."""
    path = fspath(fastq)
    opener = gzip.open if path.lower().endswith((".gz", ".bgz")) else open
    try:
        with opener(path, "rt") as handle:
            line = handle.readline()
    except (OSError, UnicodeDecodeError):
        return False
    return line.startswith("@") and "\ttp:B:" in line


@dataclass(frozen=True)
class FlowData:
    """What an alignment offers flow-aware tools."""

    ultima: bool = False
    flow_order: bool = False
    flow_tags: bool = False

    @property
    def flow_aware(self) -> bool:
        """GATK's flow mode and DeepVariant's Ultima model can use the reads."""
        return self.ultima and self.flow_order and self.flow_tags


def flow_data(
    alignment: PathInput, header: str, reference: PathInput | None = None
) -> FlowData:
    if not is_ultima_header(header):
        return FlowData()
    return FlowData(
        ultima=True,
        flow_order=has_flow_order(header),
        flow_tags=reads_have_flow_tags(alignment, reference),
    )


def flow_tag_filter() -> list[str]:
    """A pipeline stage reversing tp/t0 on the reverse-strand records of a SAM
    stream (this module, run by this Python)."""
    return [sys.executable, "-m", "wgsextract_cli.core.ultima"]


def alignment_flow_data(alignment: PathInput, reference: PathInput | None) -> FlowData:
    """flow_data for a BAM/CRAM, warning when Ultima reads lack what
    flow-aware callers need."""
    from wgsextract_cli.core.alignment_metadata import get_bam_header

    data = flow_data(alignment, get_bam_header(alignment), reference)
    if data.ultima and not data.flow_aware:
        missing = "flow order (FO)" if not data.flow_order else "flow tags (tp, t0)"
        logging.warning(
            f"{alignment} is from an Ultima sequencer but its reads have no "
            f"{missing}, so they are called as ordinary short reads. Realigning "
            "the sequencer's own BAM/CRAM with `wgsextract realign` keeps them."
        )
    return data


def warn_no_flow_model(tool: str) -> None:
    logging.warning(
        f"{tool} has no model of Ultima's homopolymer-length errors and calls "
        "extra indels in homopolymers. `vcf deepvariant`, `vcf gatk` and `vcf "
        "snp`/`indel` (bcftools' Ultima profile) handle Ultima reads."
    )


def reverse_flow_tags(record: bytes) -> bytes:
    """A SAM line with tp and t0 reversed when the read is on the reverse strand."""
    if record.startswith(b"@"):
        return record
    flag_start = record.find(b"\t") + 1
    flag = record[flag_start : record.find(b"\t", flag_start)]
    if not flag.isdigit() or not int(flag) & 0x10:
        return record
    if b"\ttp:B:" not in record and b"\tt0:Z:" not in record:
        return record
    newline = b"\n" if record.endswith(b"\n") else b""
    fields = record.rstrip(b"\r\n").split(b"\t")
    for i in range(11, len(fields)):
        prefix = fields[i][:5]
        if prefix == b"tp:B:":
            subtype, *values = fields[i][5:].split(b",")
            fields[i] = prefix + b",".join([subtype, *reversed(values)])
        elif prefix == b"t0:Z:":
            fields[i] = prefix + fields[i][5:][::-1]
    return b"\t".join(fields) + newline


def main() -> None:
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        out.write(reverse_flow_tags(line))
    out.flush()


if __name__ == "__main__":
    main()
