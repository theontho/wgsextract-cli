from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.core.variant_files import popen

from .annotation_resources import (
    GNOMAD_URLS,
    PHYLOP_URLS,
    ensure_bgzf,
    wait_with_cancel,
)
from .ref_library import (
    download_file,
    get_genome_status,
)


def download_phylop(reflib_dir, cancel_event=None, progress_callback=None):
    """Downloads and indexes PhyloP conservation scores for hg19 and hg38."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in PHYLOP_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        # We use .tsv.gz suffix for consistency in our app's search logic
        dest_path = os.path.join(target_dir, f"phylop_{build}.tsv.gz")
        logging.info(f"Downloading PhyloP {build} from Annovar mirrors...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        # Ensure BGZF format before indexing (Annovar mirrors are usually standard gzip)
        dest_path = ensure_bgzf(dest_path, None, cancel_event) or dest_path

        # Index the TSV
        logging.info(f"Indexing PhyloP {build}...")
        try:
            # Annovar PhyloP format: #Chr, Start, End, Score
            # We use bcftools annotate with CHROM=1, POS=2
            run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", dest_path])
        except Exception as e:
            logging.error(f"Failed to index PhyloP {build}: {e}")
            success = False

    return success


def download_gnomad(reflib_dir, cancel_event=None, progress_callback=None):
    """Downloads and indexes gnomAD sites VCFs for hg19 and hg38."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)

    success = True
    for build, url in GNOMAD_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        dest_path = os.path.join(target_dir, f"gnomad_{build}.vcf.bgz")
        logging.info(f"Downloading gnomAD {build} from Google Storage...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        # Index the VCF
        logging.info(f"Indexing gnomAD {build}...")
        try:
            # gnomAD VCFs are usually already bgzipped and indexed,
            # but we might need to download the index or recreate it.
            # Tabix -p vcf works for .vcf.bgz as well.
            run_command(["tabix", "-p", "vcf", "-f", dest_path])
        except Exception as e:
            logging.error(f"Failed to index gnomAD {build}: {e}")
            success = False

    return success


def delete_genome(final_name: str, reflib_dir: str):
    base_path = os.path.join(reflib_dir, "genomes", final_name)
    for ext in ["", ".partial", ".fai", ".gzi", ".dict"]:
        p = base_path + ext
        if os.path.exists(p):
            os.remove(p)
    prefix = re.sub(r"\.(fasta|fna|fa)\.gz$", "", base_path)
    for ext in ["_ncnt.csv", "_nbin.csv", ".wgse"]:
        p = prefix + ext
        if os.path.exists(p):
            os.remove(p)
    return True


def delete_ref_index(final_name: str, reflib_dir: str):
    """Deletes only the index and companion files for a reference genome."""
    base_path = os.path.join(reflib_dir, "genomes", final_name)
    # Delete index files but NOT the main genome file ("")
    for ext in [".fai", ".gzi", ".dict"]:
        p = base_path + ext
        if os.path.exists(p):
            os.remove(p)
    return True


def has_ref_ns(final_name: str, reflib_dir: str) -> bool:
    """Checks if N-count files exist for a reference genome."""
    if not reflib_dir:
        return False
    base_path = os.path.join(reflib_dir, "genomes", final_name)
    prefix = re.sub(r"\.(fasta|fna|fa)\.gz$", "", base_path)
    for ext in ["_ncnt.csv", "_nbin.csv"]:
        if os.path.exists(prefix + ext):
            return True
    return False


def delete_ref_ns(final_name: str, reflib_dir: str):
    """Deletes only the N-count CSV files for a reference genome."""
    base_path = os.path.join(reflib_dir, "genomes", final_name)
    prefix = re.sub(r"\.(fasta|fna|fa)\.gz$", "", base_path)
    for ext in ["_ncnt.csv", "_nbin.csv"]:
        p = prefix + ext
        if os.path.exists(p):
            os.remove(p)
    return True


class ReferenceAnalyzer:
    def __init__(self, fasta_path, dict_path):
        self.fasta_path = fasta_path
        self.dict_path = dict_path

    def analyze(self):
        # Simplified for now
        pass


class ReferenceCataloger:
    def __init__(self, fasta_path, dict_path):
        self.fasta_path = fasta_path
        self.dict_path = dict_path

    def update_catalog(self):
        # Simplified for now
        pass


def process_reference_file(
    fasta_path: str,
    status_callback: Callable[[str], None] | None = None,
    cancel_event: Any | None = None,
):
    logging.info(f"Processing reference: {fasta_path}")

    bgzf_path = ensure_bgzf(fasta_path, status_callback, cancel_event)
    if not bgzf_path:
        return False

    if cancel_event and cancel_event.is_set():
        return False

    base_name = re.sub(r"\.(fasta|fna|fa)\.gz$", "", bgzf_path)
    dict_path = base_name + ".dict"
    try:
        logging.info("Generating sequence dictionary...")
        if status_callback:
            status_callback("Processing: Generating Dictionary...")
        p_dict = popen(
            ["samtools", "dict", bgzf_path, "-o", dict_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not wait_with_cancel(p_dict, cancel_event):
            return False

        logging.info("Indexing FASTA...")
        if status_callback:
            status_callback("Processing: Indexing FASTA...")
        p_faidx = popen(
            ["samtools", "faidx", bgzf_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not wait_with_cancel(p_faidx, cancel_event):
            return False

    except Exception as e:
        if sys.platform == "win32" and isinstance(e, FileNotFoundError):
            logging.warning(
                f"Warning: Could not index reference on Windows (missing tools): {e}"
            )
            logging.warning("Reference is downloaded but remains unindexed.")
            return True
        logging.error(f"Indexing failed: {e}")
        return False

    analyzer = ReferenceAnalyzer(bgzf_path, dict_path)
    analyzer.analyze()
    cataloger = ReferenceCataloger(bgzf_path, dict_path)
    cataloger.update_catalog()
    return True


def download_and_process_genome(
    genome_data: dict,
    reflib_dir: str,
    interactive: bool = True,
    progress_callback: Callable | None = None,
    cancel_event: Any | None = None,
    restart: bool = False,
    status_callback: Callable[[str], None] | None = None,
):
    verify_dependencies(["samtools", "bgzip", "gzip"])
    target_dir = os.path.join(reflib_dir, "genomes")
    os.makedirs(target_dir, exist_ok=True)
    final_path = os.path.join(target_dir, genome_data["final"])
    partial_path = final_path + ".partial"

    if restart:
        if os.path.exists(final_path):
            os.remove(final_path)
        if os.path.exists(partial_path):
            os.remove(partial_path)

    status = get_genome_status(genome_data["final"], reflib_dir)
    if status == "installed":
        if not interactive:
            return process_reference_file(final_path, status_callback, cancel_event)
        print(f"\n{genome_data['final']} is already installed.")
        choice = input("Re-download anyway? [y/N]: ").strip().lower()
        if choice != "y":
            return True
        os.remove(final_path)
    elif status == "incomplete" and interactive:
        print(f"\n{genome_data['final']} is incomplete.")
        choice = input("[R]esume, [D]elete, or [C]ancel? ").strip().lower()
        if choice == "c":
            return False
        if choice == "d":
            delete_genome(genome_data["final"], reflib_dir)
            return False
        # 'r' continues to download_file below

    logging.info(f"Downloading {genome_data['label']} from {genome_data['source']}...")

    success = download_file(
        genome_data["url"], final_path, progress_callback, cancel_event
    )
    if not success:
        return False

    return process_reference_file(final_path, status_callback, cancel_event)


def download_bootstrap(
    reflib_dir: str,
    cancel_event: Any | None = None,
    progress_callback: Callable[[int, int, float], None] | None = None,
) -> bool:
    """Downloads and extracts the reference library bootstrap."""
    from wgsextract_cli.core.constants import BOOTSTRAP_FILENAME, BOOTSTRAP_URL

    if not os.path.exists(reflib_dir):
        os.makedirs(reflib_dir, exist_ok=True)

    dest_path = os.path.join(reflib_dir, BOOTSTRAP_FILENAME)
    logging.info(f"Downloading bootstrap from {BOOTSTRAP_URL}...")

    if not download_file(BOOTSTRAP_URL, dest_path, progress_callback, cancel_event):
        return False

    logging.info(f"Extracting bootstrap to {reflib_dir}...")
    try:
        # Use tar -xvf -z (or bgzip -d | tar)
        # Since we use bgzip, we can pipe bgzip -d to tar -xf -
        # Use tar -xkf (keep existing files) and --no-xattrs to avoid macOS metadata warnings
        cmd = ["tar", "-xkf", dest_path, "--no-xattrs", "-C", reflib_dir]
        # Fallback if --no-xattrs is not supported (e.g. on very old tar)
        try:
            run_command(cmd, capture_output=True)
        except subprocess.CalledProcessError:
            cmd = ["tar", "-xkf", dest_path, "-C", reflib_dir]
            run_command(cmd)

        # Cleanup the archive after successful extraction
        os.remove(dest_path)
        logging.info("Bootstrap extraction complete.")
        return True
    except Exception as e:
        logging.error(f"Failed to extract bootstrap: {e}")
        return False
