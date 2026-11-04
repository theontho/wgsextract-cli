"""Warn when a command succeeds with an empty result.

An empty VCF or BAM is a valid file, so the tools exit 0; but a whole sample's
chrM with no variants, or a VCF none of whose variants an annotation scores,
usually means the reference or build doesn't match the input. These checks say
so, so the command doesn't look like it worked.
"""

import os
import subprocess

from wgsextract_cli.core.cram_counts import cram_container_totals
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.results import current_result, warn
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.utils import popen, run_command

# Fewer variants than this can legitimately miss every annotation (a chrM VCF
# has no ClinVar hits); a whole sample's VCF can't.
MIN_VARIANTS_TO_EXPECT_ANNOTATION = 1000


def vcf_record_count(vcf: PathInput) -> int | None:
    """Known indexed record count, or None when unknown/unreadable.

    htsjdk indexes omit counts. Confirm a reported zero with one record,
    retaining None rather than inventing a count for a nonempty VCF.
    """
    try:
        res = run_command(
            ["bcftools", "index", "--nrecords", str(vcf)],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    try:
        count = int(res.stdout.strip()) if res.returncode == 0 else None
    except ValueError:
        return None
    if count != 0:
        return count
    try:
        proc = popen(
            ["bcftools", "view", "-H", str(vcf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    return 0 if _first_line_or_none(proc) is False else None


def bam_has_reads(bam: PathInput) -> bool | None:
    """Whether a BAM/CRAM has any read, or None if it can't be read. Uses the
    index's counts when there is one, else stops at the first read."""
    if alignment_index(bam):
        try:
            res = run_command(
                ["samtools", "idxstats", str(bam)], capture_output=True, check=False
            )
        except OSError:
            return None
        if res.returncode == 0:
            rows = [line.split("\t") for line in res.stdout.splitlines()]
            return any(int(r[2]) + int(r[3]) for r in rows if len(r) >= 4)
    try:
        proc = popen(
            ["samtools", "view", str(bam)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    return _first_line_or_none(proc)


def alignment_record_counts(bam: PathInput) -> dict[str, int]:
    """Record totals of an indexed BAM/CRAM, read without decoding its reads.

    A BAM's index gives ``mapped_records`` and ``unmapped_records``; a CRAM's
    container headers give only the ``records`` total (idxstats would decode a
    whole CRAM). Empty when neither is available.
    """
    if not alignment_index(bam):
        return {}
    if str(bam).lower().endswith(".cram"):
        totals = cram_container_totals(bam)
        if totals is None:
            return {}
        return {"records": sum(records for records, _bases in totals.values())}
    try:
        res = run_command(
            ["samtools", "idxstats", str(bam)], capture_output=True, check=False
        )
    except OSError:
        return {}
    if res.returncode != 0:
        return {}
    rows = [line.split("\t") for line in res.stdout.splitlines()]
    mapped = sum(int(r[2]) for r in rows if len(r) >= 4)
    unmapped = sum(int(r[3]) for r in rows if len(r) >= 4)
    return {
        "records": mapped + unmapped,
        "mapped_records": mapped,
        "unmapped_records": unmapped,
    }


def record_alignment_output(
    path: PathInput, role: str = "alignment", *, hint: str = ""
) -> dict[str, int]:
    """Register a written BAM/CRAM and its record counts on the result, warning
    when it has no reads; returns the counts."""
    counts = alignment_record_counts(path)
    current_result().add_output(path, role, metrics=dict(counts))
    if counts.get("records") == 0:
        warn(
            "no_reads",
            f"{path} has no reads" + (f": {hint}" if hint else "."),
            path=os.path.abspath(path),
        )
    return counts


def _first_line_or_none(proc: subprocess.Popen[str]) -> bool | None:
    """Whether a process printed a line, stopping it at the first; None if it
    failed before printing one."""
    assert proc.stdout is not None
    found = bool(proc.stdout.readline())
    if found:
        proc.kill()
    proc.stdout.close()
    code = proc.wait()
    return True if found else (False if code == 0 else None)


def warn_if_no_variants(
    vcf: PathInput, ref: str | None = None, region: str | None = None
) -> int | None:
    """Warn when a variant caller wrote no records; return the record count
    (None when unknown)."""
    count = vcf_record_count(vcf)
    if count != 0:
        return count
    where = f" in {region}" if region else ""
    hint = (
        f"check that the reference ({ref}) is the one the input was aligned to"
        if ref
        else "check the input's reference build"
    )
    warn(
        "no_variants",
        f"No variants were called{where} ({vcf}). If the input has reads there, {hint}.",
        path=str(vcf),
        region=region,
    )
    return 0


def warn_if_no_reads(bam: PathInput, what: str, hint: str = "") -> None:
    """Warn when an extraction wrote no reads."""
    if bam_has_reads(bam) is not False:
        return
    warn(
        "no_reads",
        f"{bam} has no reads: the input has no {what}"
        + (f" ({hint})" if hint else "")
        + ", or names that chromosome differently.",
        path=str(bam),
    )


def vcf_has_info(vcf: PathInput, tag: str) -> bool | None:
    """Whether any record has INFO/<tag>, reading only until the first one."""
    try:
        proc = popen(
            ["bcftools", "view", "-H", "-i", f'INFO/{tag}!="."', str(vcf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    return _first_line_or_none(proc)


def annotation_tags(columns: str) -> list[str]:
    """The INFO tags a bcftools annotate -c list writes: bare names (CLNSIG),
    INFO/ names, and renames' destinations (INFO/GNOMAD_AF:=INFO/AF)."""
    fixed = {"CHROM", "POS", "FROM", "TO", "BEG", "END", "REF", "ALT", "ID"}
    fixed |= {"QUAL", "FILTER", "INFO", "FORMAT", "-"}
    tags = []
    for column in columns.split(","):
        name = column.split(":=", 1)[0].lstrip("+-=.")
        name = name.removeprefix("INFO/")
        if name and name.upper() not in fixed and not name.startswith("FMT/"):
            tags.append(name)
    return tags


def warn_if_nothing_annotated(vcf: PathInput, columns: str, label: str) -> None:
    """Warn when a large VCF got none of an annotation's INFO values."""
    tags = annotation_tags(columns)
    if not tags:
        return
    total = vcf_record_count(vcf)
    if total is None or total < MIN_VARIANTS_TO_EXPECT_ANNOTATION:
        return
    if vcf_has_info(vcf, tags[0]) is not False:
        return
    warn(
        "nothing_annotated",
        f"None of the {total:,} variants in {vcf} got a {label} value. Check that "
        f"the VCF's genome build matches the {label} data.",
        path=str(vcf),
        annotation=label,
        variants=total,
    )
