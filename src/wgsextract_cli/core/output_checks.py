"""Warn when a command succeeds with an empty result.

An empty VCF or BAM is a valid file, so the tools exit 0; but a whole sample's
chrM with no variants, or a VCF none of whose variants an annotation scores,
usually means the reference or build doesn't match the input. These checks say
so, so the command doesn't look like it worked.
"""

import logging
import subprocess

from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.utils import popen, run_command

# Fewer variants than this can legitimately miss every annotation (a chrM VCF
# has no ClinVar hits); a whole sample's VCF can't.
MIN_VARIANTS_TO_EXPECT_ANNOTATION = 1000


def vcf_record_count(vcf: PathInput) -> int | None:
    """Records in an indexed VCF (from its index), or None if it can't be read."""
    try:
        res = run_command(
            ["bcftools", "index", "--nrecords", str(vcf)],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    try:
        return int(res.stdout.strip()) if res.returncode == 0 else None
    except ValueError:
        return None


def bam_read_count(bam: PathInput) -> int | None:
    """Reads (mapped or not) in a BAM/CRAM, or None if it can't be read."""
    try:
        res = run_command(
            ["samtools", "view", "-c", str(bam)], capture_output=True, check=False
        )
    except OSError:
        return None
    try:
        return int(res.stdout.strip()) if res.returncode == 0 else None
    except ValueError:
        return None


def warn_if_no_variants(
    vcf: PathInput, ref: str | None = None, region: str | None = None
) -> None:
    """Warn when a variant caller wrote no records."""
    if vcf_record_count(vcf) != 0:
        return
    where = f" in {region}" if region else ""
    hint = (
        f"check that the reference ({ref}) is the one the input was aligned to"
        if ref
        else "check the input's reference build"
    )
    logging.warning(
        f"No variants were called{where} ({vcf}). If the input has reads there, {hint}."
    )


def warn_if_no_reads(bam: PathInput, what: str, hint: str = "") -> None:
    """Warn when an extraction wrote no reads."""
    if bam_read_count(bam) != 0:
        return
    logging.warning(
        f"{bam} has no reads: the input has no {what}"
        + (f" ({hint})" if hint else "")
        + ", or names that chromosome differently."
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
    assert proc.stdout is not None
    found = bool(proc.stdout.readline())
    if found:
        proc.kill()
    proc.stdout.close()
    code = proc.wait()
    return True if found else (False if code == 0 else None)


def warn_if_nothing_annotated(vcf: PathInput, columns: str, label: str) -> None:
    """Warn when a large VCF got none of an annotation's INFO values."""
    tags = [c.split("/", 1)[1] for c in columns.split(",") if c.startswith("INFO/")]
    if not tags:
        return
    total = vcf_record_count(vcf)
    if total is None or total < MIN_VARIANTS_TO_EXPECT_ANNOTATION:
        return
    if vcf_has_info(vcf, tags[0]) is not False:
        return
    logging.warning(
        f"None of the {total:,} variants in {vcf} got a {label} value. Check that "
        f"the VCF's genome build matches the {label} data."
    )
