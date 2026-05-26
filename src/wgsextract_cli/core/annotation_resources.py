from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Protocol

from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import popen

from .ref_library import (
    download_file,
)


class CancelEvent(Protocol):
    def is_set(self) -> bool: ...


ProgressCallback = Callable[[int, int, float], None]


def get_genome_size(final_name: str, reflib_dir: str) -> str:
    """Returns human-readable local size of the genome and its index files."""
    if not reflib_dir:
        return ""
    base_path = os.path.join(reflib_dir, "genomes", final_name)
    total_bytes = 0
    # Include .partial, .fai, .gzi, .dict etc
    for ext in ["", ".partial", ".fai", ".gzi", ".dict"]:
        p = base_path + ext
        if os.path.exists(p):
            total_bytes += os.path.getsize(p)

    if total_bytes == 0:
        return ""
    if total_bytes > 1024 * 1024 * 1024:
        return f"{total_bytes / (1024 * 1024 * 1024):.1f} GB"
    return f"{total_bytes / (1024 * 1024):.1f} MB"


CLINVAR_URLS = {
    "hg38": "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz",
    "hg19": "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh37/clinvar.vcf.gz",
}


REVEL_URLS = {
    "hg38": "http://www.openbioinformatics.org/annovar/download/hg38_revel.txt.gz",
    "hg19": "http://www.openbioinformatics.org/annovar/download/hg19_revel.txt.gz",
}


PHYLOP_URLS = {
    "hg38": "http://www.openbioinformatics.org/annovar/download/hg38_phyloP100way.txt.gz",
    "hg19": "http://www.openbioinformatics.org/annovar/download/hg19_phyloP100way.txt.gz",
}


GNOMAD_URLS = {
    "hg38": "https://storage.googleapis.com/gcp-public-data--gnomad/release/2.1.1/liftover_grch38/vcf/exomes/gnomad.exomes.r2.1.1.sites.liftover_grch38.vcf.bgz",
    "hg19": "https://storage.googleapis.com/gcp-public-data--gnomad/release/2.1.1/vcf/exomes/gnomad.exomes.r2.1.1.sites.vcf.bgz",
}


SPLICEAI_URLS = {
    "hg38": "https://basespace.illumina.com/s/vU97u6757PBt/spliceai_scores.raw.snv.hg38.vcf.gz",  # Note: Requires login/mirror check
    "hg19": "https://basespace.illumina.com/s/vU97u6757PBt/spliceai_scores.raw.snv.hg19.vcf.gz",
}


ALPHAMISSENSE_URLS = {
    "hg38": "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz",
    "hg19": "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg19.tsv.gz",
}


PHARMGKB_URLS = {
    "hg38": "https://api.pharmgkb.org/v1/download/file/data/annotations.zip",
}


def download_clinvar(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes official ClinVar VCFs for hg19 and hg38."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in CLINVAR_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        dest_path = os.path.join(target_dir, f"clinvar_{build}.vcf.gz")
        logging.info(f"Downloading ClinVar {build} from NIH FTP...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        # Index the VCF
        logging.info(f"Indexing ClinVar {build}...")
        try:
            # We need tabix
            run_command(["tabix", "-p", "vcf", "-f", dest_path])
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Failed to index ClinVar {build}: {e}")
            success = False

    return success


def download_spliceai(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes SpliceAI precomputed scores."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in SPLICEAI_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        dest_path = os.path.join(target_dir, f"spliceai_{build}.vcf.gz")
        logging.info(f"Downloading SpliceAI {build}...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        logging.info(f"Indexing SpliceAI {build}...")
        try:
            run_command(["tabix", "-p", "vcf", "-f", dest_path])
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Failed to index SpliceAI {build}: {e}")
            success = False

    return success


def download_alphamissense(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes AlphaMissense pathogenicity scores."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in ALPHAMISSENSE_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        dest_path = os.path.join(target_dir, f"alphamissense_{build}.tsv.gz")
        logging.info(f"Downloading AlphaMissense {build} from Google Storage...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        logging.info(f"Indexing AlphaMissense {build}...")
        try:
            # AlphaMissense TSV format: #CHROM, POS, REF, ALT, am_pathogenicity, am_class
            # We want CHROM=1, POS=2
            run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", dest_path])
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Failed to index AlphaMissense {build}: {e}")
            success = False

    return success


def download_pharmgkb(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads PharmGKB annotations (placeholder for full implementation)."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in PHARMGKB_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        # PharmGKB is often a ZIP, but for now we'll just download it
        dest_path = os.path.join(target_dir, f"pharmgkb_{build}.zip")
        logging.info(f"Downloading PharmGKB {build}...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

    return success


def wait_with_cancel(
    process: subprocess.Popen[str] | subprocess.Popen[bytes],
    cancel_event: CancelEvent | None = None,
) -> bool:
    """Waits for a process while checking for a cancel event."""
    while process.poll() is None:
        if cancel_event and cancel_event.is_set():
            logging.info(f"Terminating process {process.pid} due to cancellation.")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            return False
        time.sleep(0.1)
    return process.returncode == 0


def is_bgzf(path: str) -> bool:
    """Check if a file is in BGZF format by looking for the magic bytes."""
    try:
        with open(path, "rb") as f:
            # BGZF header: 1f 8b 08 04 ... (the 4th byte 04 indicates XLEN is present)
            # This is a bit simplified but generally reliable for our tools.
            header = f.read(4)
            return header == b"\x1f\x8b\x08\x04"
    except OSError as e:
        raise WGSExtractError(f"Failed to inspect gzip header for {path}: {e}") from e


def ensure_bgzf(
    path: str,
    status_callback: Callable[[str], None] | None = None,
    cancel_event: CancelEvent | None = None,
) -> str | None:
    if is_bgzf(path):
        logging.info(f"{path} is already in BGZF format.")
        return path

    if cancel_event and cancel_event.is_set():
        return None

    logging.info(f"Recompressing {path} to BGZF format (required for fast access)...")
    if status_callback:
        status_callback("Processing: Recompressing (BGZF)...")

    tmp_path = path + ".tmp.gz"
    try:
        if path.endswith(".gz"):
            with open(tmp_path, "wb") as f_out:
                p1 = popen(["gunzip", "-c", path], stdout=subprocess.PIPE)
                p2 = popen(["bgzip", "-c"], stdin=p1.stdout, stdout=f_out)
                if p1.stdout:
                    p1.stdout.close()

                if not wait_with_cancel(p2, cancel_event):
                    p1.terminate()
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    return None
                gunzip_returncode = p1.wait()
                if gunzip_returncode != 0:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise WGSExtractError(
                        f"gunzip failed with return code {gunzip_returncode}."
                    )

            os.remove(path)
            os.rename(tmp_path, path)
            logging.info(f"Recompression of {path} complete.")
            return path
        else:
            with open(tmp_path, "wb") as f_out:
                p_bgzip = popen(["bgzip", "-c", path], stdout=f_out)
                if not wait_with_cancel(p_bgzip, cancel_event):
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    return None

            os.remove(path)
            new_path = path + ".gz" if not path.endswith(".gz") else path
            os.rename(tmp_path, new_path)
            logging.info(f"Recompression to {new_path} complete.")
            return new_path
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        if sys.platform == "win32" and isinstance(e, FileNotFoundError):
            logging.warning(
                f"Warning: Could not recompress to BGZF on Windows (missing tools): {e}"
            )
            return path
        logging.error(f"Recompression failed: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return None


def download_revel(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes REVEL pathogenicity scores for hg19 and hg38."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in REVEL_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        # We use .tsv.gz suffix for consistency in our app's search logic
        dest_path = os.path.join(target_dir, f"revel_{build}.tsv.gz")
        logging.info(f"Downloading REVEL {build} from Annovar mirrors...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        # Ensure BGZF format before indexing (Annovar mirrors are usually standard gzip)
        dest_path = ensure_bgzf(dest_path, None, cancel_event) or dest_path

        # Index the TSV
        logging.info(f"Indexing REVEL {build}...")
        try:
            # Annovar REVEL format: #Chr, Start, End, Ref, Alt, REVEL...
            # We want CHROM=1, POS=2, REF=4, ALT=5
            run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", dest_path])
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Failed to index REVEL {build}: {e}")
            success = False

    return success
