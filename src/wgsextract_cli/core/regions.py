import logging
import os
import tempfile

try:
    import psutil
except ImportError:
    psutil = None


from .alignment_metadata import (
    get_bam_header,
)
from .utils import (
    run_command,
)


def is_long_read(bam_path, cram_opt=None, header=None):
    """
    Rough heuristic to detect long-read data (e.g. Nanopore) from BAM header.
    In the original app, it checks if read length > 500.
    """
    if header is None:
        header = get_bam_header(bam_path, cram_opt)
    if not header:
        return False
    if "PL:ONT" in header or "PL:PACBIO" in header:
        return True
    return False


def get_vcf_chr_name(vcf_path, target_chr):
    """Map standard chromosome to VCF-specific naming."""
    try:
        from wgsextract_cli.core.dependencies import get_tool_path

        bcftools = get_tool_path("bcftools")
        res = run_command([bcftools, "index", "-s", vcf_path], capture_output=True)
        v_chroms = [line.split("\t")[0] for line in res.stdout.strip().split("\n")]

        if target_chr.upper() in ["M", "MT", "CHRM", "CHRMT"]:
            for c in ["chrM", "chrMT", "MT", "M"]:
                if c in v_chroms:
                    return c
        elif target_chr.upper() in ["Y", "CHRY"]:
            for c in ["chrY", "Y"]:
                if c in v_chroms:
                    return c
    except Exception as e:
        logging.warning(f"VCF chromosome name detection failed for {vcf_path}: {e}")
    return target_chr


def get_region_bed(region_str):
    """
    Converts a region string (chr:start-end) to a temporary BED file.
    Returns the path to the temporary file.
    """
    if not region_str:
        return None

    chrom = region_str
    start = 0
    end = 500000000  # Large default

    if ":" in region_str:
        chrom, coords = region_str.split(":")
        if "-" in coords:
            start_s, end_s = coords.split("-")
            start = int(start_s)
            end = int(end_s)
        else:
            try:
                start = int(coords)
                end = start + 1
            except ValueError:
                # Could be just a chrom name that happens to have a colon?
                # Unlikely in bio, but let's be safe.
                pass

    fd, path = tempfile.mkstemp(suffix=".bed")
    with os.fdopen(fd, "w") as f:
        f.write(f"{chrom}\t{max(0, start - 1)}\t{end}\n")
    return path
