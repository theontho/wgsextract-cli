from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from wgsextract_cli.core.download_progress import CancelEvent, ProgressCallback
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command

from .ref_library import (
    download_file,
)

__all__ = ["CancelEvent", "ProgressCallback"]


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


# SpliceAI precomputed scores sit behind an Illumina BaseSpace login, so they
# cannot be fetched automatically (BaseSpace answers with an HTML page).
SPLICEAI_URLS: dict[str, str | None] = {"hg38": None, "hg19": None}
SPLICEAI_MANUAL_HINT = (
    "SpliceAI precomputed scores require an Illumina BaseSpace login and cannot "
    "be downloaded automatically. Download spliceai_scores.raw.snv.{build}.vcf.gz "
    "via https://github.com/Illumina/SpliceAI (see 'Precomputed scores'), save it "
    "as {dest} (or set spliceai_vcf_path in config.toml), then rerun "
    "'wgsextract ref spliceai' to index it."
)


ALPHAMISSENSE_URLS = {
    "hg38": "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz",
    "hg19": "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg19.tsv.gz",
}


PHARMGKB_URLS = {
    "hg38": "https://api.clinpgx.org/v1/download/file/data/annotations.zip",
}

# tabix arguments for a VCF and for TSVs keyed on CHROM=col1, POS=col2.
_TABIX_VCF = ("-p", "vcf")
_TABIX_POS_TSV = ("-s", "1", "-b", "2", "-e", "2")


@dataclass(frozen=True)
class _AnnotationResource:
    label: str
    urls: Mapping[str, str | None]
    dest_name: str  # formatted with {build}
    tabix_args: tuple[str, ...] | None  # None: not indexed
    manual_hint: str | None = None  # for resources without a download URL


_CLINVAR = _AnnotationResource(
    "ClinVar", CLINVAR_URLS, "clinvar_{build}.vcf.gz", _TABIX_VCF
)
_SPLICEAI = _AnnotationResource(
    "SpliceAI",
    SPLICEAI_URLS,
    "spliceai_{build}.vcf.gz",
    _TABIX_VCF,
    SPLICEAI_MANUAL_HINT,
)
_ALPHAMISSENSE = _AnnotationResource(
    "AlphaMissense",
    ALPHAMISSENSE_URLS,
    "alphamissense_{build}.tsv.gz",
    _TABIX_POS_TSV,
)
# PharmGKB ships a ZIP of TSVs; it is downloaded as-is.
_PHARMGKB = _AnnotationResource("PharmGKB", PHARMGKB_URLS, "pharmgkb_{build}.zip", None)
# Annovar REVEL TSV: #Chr, Start, End, Ref, Alt, REVEL (.tsv.gz for the resolver).
_REVEL = _AnnotationResource(
    "REVEL", REVEL_URLS, "revel_{build}.tsv.gz", _TABIX_POS_TSV
)


def _has_gzip_magic(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def _download_resource(
    resource: _AnnotationResource,
    reflib_dir: str,
    cancel_event: CancelEvent | None,
    progress_callback: ProgressCallback | None,
) -> bool:
    """Download (or pick up a manual copy of) each build, then BGZF + tabix it."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    success = True
    for build, url in resource.urls.items():
        if cancelled():
            return False
        label = f"{resource.label} {build}"
        dest_path = os.path.join(target_dir, resource.dest_name.format(build=build))

        if url is None:
            if not os.path.exists(dest_path):
                hint = resource.manual_hint or "{dest} must be downloaded manually."
                logging.error(hint.format(build=build, dest=dest_path))
                success = False
                continue
            logging.info(f"Using manually downloaded {label}: {dest_path}")
        else:
            logging.info(f"Downloading {label}...")
            if not download_file(url, dest_path, progress_callback, cancel_event):
                success = False
                continue
            if cancelled():
                return False

        if resource.tabix_args is None:
            continue
        try:
            if not _has_gzip_magic(dest_path):
                os.remove(dest_path)
                raise WGSExtractError(
                    f"{dest_path} is not gzip data (an error or login page?); removed."
                )
            bgzf_path = ensure_bgzf(dest_path, None, cancel_event)
            if bgzf_path is None:
                return False
            logging.info(f"Indexing {label}...")
            run_command(
                ["tabix", "-f", *resource.tabix_args, bgzf_path], capture_output=True
            )
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Failed to prepare {label}: {e}")
            success = False

    return success


def download_clinvar(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes official ClinVar VCFs for hg19 and hg38."""
    return _download_resource(_CLINVAR, reflib_dir, cancel_event, progress_callback)


def download_spliceai(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Indexes manually downloaded SpliceAI precomputed scores."""
    return _download_resource(_SPLICEAI, reflib_dir, cancel_event, progress_callback)


def download_alphamissense(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes AlphaMissense pathogenicity scores."""
    return _download_resource(
        _ALPHAMISSENSE, reflib_dir, cancel_event, progress_callback
    )


def download_pharmgkb(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads PharmGKB annotations (placeholder for full implementation)."""
    return _download_resource(_PHARMGKB, reflib_dir, cancel_event, progress_callback)


def download_revel(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes REVEL pathogenicity scores for hg19 and hg38."""
    return _download_resource(_REVEL, reflib_dir, cancel_event, progress_callback)


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
    """
    Recompress path to BGZF in place (a non-.gz file becomes path + ".gz").

    Returns the BGZF path, or None if cancelled; raises WGSExtractError on failure.
    """
    if is_bgzf(path):
        logging.info(f"{path} is already in BGZF format.")
        return path
    if cancel_event and cancel_event.is_set():
        return None

    logging.info(f"Recompressing {path} to BGZF format (required for fast access)...")
    if status_callback:
        status_callback("Processing: Recompressing (BGZF)...")

    is_gz = path.endswith(".gz")
    new_path = path if is_gz else path + ".gz"
    tmp_path = path + ".tmp.gz"
    procs: list[subprocess.Popen[bytes]] = []
    try:
        with open(tmp_path, "wb") as f_out:
            if is_gz:
                p_gzip = popen(["gzip", "-dc", path], stdout=subprocess.PIPE)
                procs.append(p_gzip)
                procs.append(popen(["bgzip", "-c"], stdin=p_gzip.stdout, stdout=f_out))
                if p_gzip.stdout:
                    p_gzip.stdout.close()
            else:
                procs.append(popen(["bgzip", "-c", path], stdout=f_out))
            wait_with_cancel(procs[-1], cancel_event)
            if cancel_event and cancel_event.is_set():
                return None
            failed = [f"exit {p.wait()}" for p in procs if p.wait() != 0]
        if failed:
            raise WGSExtractError(
                f"BGZF recompression of {path} failed ({', '.join(failed)})."
            )
        os.replace(tmp_path, new_path)
        if new_path != path:
            os.remove(path)
    except FileNotFoundError as e:
        if sys.platform == "win32":
            logging.warning(
                f"Could not recompress to BGZF on Windows (missing tools): {e}"
            )
            return path
        raise WGSExtractError(f"BGZF recompression of {path} failed: {e}") from e
    except OSError as e:
        raise WGSExtractError(f"BGZF recompression of {path} failed: {e}") from e
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    logging.info(f"Recompression to {new_path} complete.")
    return new_path
