from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from wgsextract_cli.core.bgzf import ensure_bgzf, is_bgzf
from wgsextract_cli.core.bigwig_resources import (
    download_gerp,
    download_phylop,
    open_bigwig,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.download_progress import (
    CancelEvent,
    ProgressCallback,
    format_bytes,
)
from wgsextract_cli.core.gnomad_af import build_gnomad_af
from wgsextract_cli.core.pharmgkb import download_pharmgkb
from wgsextract_cli.core.utils import (
    WGSExtractError,
    popen,
    run_command,
    wait_with_cancel,
)

from .ref_library import (
    download_file,
)

__all__ = [
    "CancelEvent",
    "ProgressCallback",
    "build_gnomad_af",
    "download_gerp",
    "download_pharmgkb",
    "download_phylop",
    "ensure_bgzf",
    "is_bgzf",
    "open_bigwig",
    "wait_with_cancel",
]


def get_genome_size(final_name: str, reflib_dir: str) -> str:
    """Returns human-readable local size of the genome and its index files."""
    if not reflib_dir:
        return ""
    base_path = str(Path(reflib_dir, "genomes", final_name))
    total_bytes = 0
    # Include .partial, .fai, .gzi, .dict etc
    for ext in ["", ".partial", ".fai", ".gzi", ".dict"]:
        p = base_path + ext
        if Path(p).exists():
            total_bytes += Path(p).stat().st_size

    return format_bytes(total_bytes) if total_bytes else ""


CLINVAR_URLS = {
    "hg38": "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz",
    "hg19": "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh37/clinvar.vcf.gz",
}


REVEL_URLS = {
    "hg38": "http://www.openbioinformatics.org/annovar/download/hg38_revel.txt.gz",
    "hg19": "http://www.openbioinformatics.org/annovar/download/hg19_revel.txt.gz",
}


GNOMAD_URLS = {
    "hg38": "https://storage.googleapis.com/gcp-public-data--gnomad/release/2.1.1/liftover_grch38/vcf/exomes/gnomad.exomes.r2.1.1.sites.liftover_grch38.vcf.bgz",
    "hg19": "https://storage.googleapis.com/gcp-public-data--gnomad/release/2.1.1/vcf/exomes/gnomad.exomes.r2.1.1.sites.vcf.bgz",
}


# SpliceAI precomputed scores. Illumina's own files sit behind a BaseSpace
# login; Ensembl publishes raw scores for GRCh38 openly, in the same VCF format
# (INFO/SpliceAI), BGZF with a .tbi: every SNV in MANE Select transcripts, no
# indels. There is no open GRCh37 set, so hg19 stays a manual download. The
# scores are free for academic and not-for-profit use only (Illumina's terms).
SPLICEAI_URLS: dict[str, str | None] = {
    "hg38": "https://ftp.ensembl.org/pub/data_files/homo_sapiens/GRCh38/"
    "variation_plugins/spliceai_scores.raw.snv.ensembl_mane_v1.4.grch38.vcf.gz",
    "hg19": None,
}
SPLICEAI_TERMS = (
    "SpliceAI scores are free for academic and not-for-profit use; other use "
    "needs a commercial license from Illumina (https://github.com/Illumina/SpliceAI)."
)
SPLICEAI_MANUAL_HINT = (
    "SpliceAI precomputed scores for {build} require an Illumina BaseSpace login "
    "and cannot be downloaded automatically. Download "
    "spliceai_scores.raw.snv.{build}.vcf.gz via https://github.com/Illumina/SpliceAI "
    "(see 'Precomputed scores'), save it as {dest} (or set spliceai_vcf_path in "
    "config.toml), then rerun 'wgsextract ref spliceai' to index it."
)


ALPHAMISSENSE_URLS = {
    "hg38": "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg38.tsv.gz",
    "hg19": "https://storage.googleapis.com/dm_alphamissense/AlphaMissense_hg19.tsv.gz",
}


# CADD v1.7 precomputed scores, BGZF with published .tbi and .md5 files:
# build -> (remote directory, gnomAD indel file). The SNV file covers every
# possible SNV (~85 GB); indels are only those seen in gnomAD.
CADD_BASE_URL = "https://krishna.gs.washington.edu/download/CADD/v1.7"
CADD_FILES = {
    "hg38": ("GRCh38", "gnomad.genomes.r4.0.indel.tsv.gz"),
    "hg19": ("GRCh37", "gnomad.genomes-exomes.r4.0.indel.tsv.gz"),
}
CADD_SNV_FILE = "whole_genome_SNVs.tsv.gz"


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
    # Downloads are BGZF with a <url>.tbi next to them: fetch that index rather
    # than recompressing and indexing tens of GB locally.
    published_index: bool = False


_CLINVAR = _AnnotationResource(
    "ClinVar", CLINVAR_URLS, "clinvar_{build}.vcf.gz", _TABIX_VCF
)
_SPLICEAI = _AnnotationResource(
    "SpliceAI",
    SPLICEAI_URLS,
    "spliceai_{build}.vcf.gz",
    _TABIX_VCF,
    SPLICEAI_MANUAL_HINT,
    published_index=True,
)
_ALPHAMISSENSE = _AnnotationResource(
    "AlphaMissense",
    ALPHAMISSENSE_URLS,
    "alphamissense_{build}.tsv.gz",
    _TABIX_POS_TSV,
)
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
    target_dir = str(Path(reflib_dir, "ref"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    success = True
    # Builds with no download URL that the user hasn't supplied: not a failure
    # while another build installs (SpliceAI hg38 downloads, hg19 is manual).
    manual_missing = 0
    for build, url in resource.urls.items():
        if cancelled():
            return False
        label = f"{resource.label} {build}"
        dest_path = str(Path(target_dir, resource.dest_name.format(build=build)))

        if url is None:
            if not Path(dest_path).exists():
                hint = resource.manual_hint or "{dest} must be downloaded manually."
                logging.warning(hint.format(build=build, dest=dest_path))
                manual_missing += 1
                continue
            logging.info(f"Using manually downloaded {label}: {dest_path}")
        elif (
            resource.published_index
            and Path(dest_path).exists()
            and not Path(dest_path + ".tbi").exists()
        ):
            # A file the user saved here (e.g. Illumina's full set): indexed
            # locally below, never treated as a partial download and replaced.
            logging.info(f"Using manually downloaded {label}: {dest_path}")
        elif resource.published_index:
            if not _download_indexed(
                label, url, dest_path, progress_callback, cancel_event
            ):
                success = False
            if cancelled():
                return False
            continue
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
                Path(dest_path).unlink()
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

    return success and manual_missing < len(resource.urls)


# The empty block every complete BGZF file ends with (SAM spec 4.1.2).
_BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")


def _download_indexed(
    label: str,
    url: str,
    dest: str,
    progress_callback: ProgressCallback | None,
    cancel_event: CancelEvent | None,
) -> bool:
    """Download a BGZF file and its published .tbi, then check them; a pair
    already in place is checked too, but never replaced or removed."""
    if not get_tool_path("tabix"):
        logging.error(f"Checking {label} needs tabix, which was not found.")
        return False
    downloaded = not (Path(dest).is_file() and Path(dest + ".tbi").is_file())
    if downloaded:
        # The data before its index, so the index is never older than the data.
        for suffix in ("", ".tbi"):
            logging.info(f"Downloading {label}{suffix and ' index'}...")
            if not download_file(
                url + suffix, dest + suffix, progress_callback, cancel_event
            ):
                return False
    try:
        _check_indexed(dest)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        if not downloaded:
            logging.error(f"{label} at {dest} failed its check: {e}")
            return False
        logging.error(f"{label} failed its check ({e}); removing it to re-download.")
        for suffix in ("", ".tbi"):
            Path(dest + suffix).unlink(missing_ok=True)
        return False
    logging.info(f"{label} is installed: {dest}")
    return True


def _check_indexed(dest: str) -> None:
    """A complete BGZF file whose index matches it: the file ends with the BGZF
    EOF block, and the last contig reads through the index to its end."""
    with open(dest, "rb") as f:
        f.seek(-len(_BGZF_EOF), 2)
        if f.read() != _BGZF_EOF:
            raise WGSExtractError("it is truncated (no BGZF end-of-file block)")
    contigs = run_command(["tabix", "-l", dest], capture_output=True).stdout.split()
    if not contigs:
        raise WGSExtractError("its index lists no contigs")
    # The last contig's data is at the end of the file.
    query = popen(
        ["tabix", dest, contigs[-1]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert query.stdout is not None and query.stderr is not None
    records = sum(1 for _ in query.stdout)
    error = query.stderr.read().strip()
    if query.wait() != 0:
        raise WGSExtractError(f"reading {contigs[-1]} failed: {error}")
    if not records:
        raise WGSExtractError(f"no records on {contigs[-1]}")


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
    """Downloads SpliceAI scores for hg38 (Ensembl) and indexes a manually
    downloaded hg19 file."""
    logging.info(SPLICEAI_TERMS)
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


def download_revel(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes REVEL pathogenicity scores for hg19 and hg38."""
    return _download_resource(_REVEL, reflib_dir, cancel_event, progress_callback)


def _verify_published_md5(path: str, url: str) -> None:
    """Check path against the <url>.md5 file published next to it."""
    with urlopen(url + ".md5", timeout=60) as response:
        expected = response.read().decode().split()[0].lower()
    logging.info(f"Verifying MD5 of {Path(path).name}...")
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        Path(path).unlink()
        raise WGSExtractError(
            f"{path} failed its MD5 check and was removed; rerun to download it again."
        )


def download_cadd(
    reflib_dir: str,
    build: str = "hg38",
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads CADD v1.7 SNV and gnomAD-indel scores, with their tabix indexes."""
    remote_dir, indel_name = CADD_FILES[build]
    target_dir = str(Path(reflib_dir, "ref"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    files = [
        (indel_name, f"cadd_indel_{build}.tsv.gz"),
        (CADD_SNV_FILE, f"cadd_{build}.tsv.gz"),
    ]
    for remote_name, local_name in files:
        dest = str(Path(target_dir, local_name))
        if Path(dest).is_file() and Path(dest + ".tbi").is_file():
            logging.info(f"{local_name} is already installed.")
            continue
        url = f"{CADD_BASE_URL}/{remote_dir}/{remote_name}"
        # The data before its index, so the index is never older than the data.
        for suffix in ("", ".tbi"):
            logging.info(f"Downloading CADD {build} {remote_name}{suffix}...")
            if not download_file(
                url + suffix, dest + suffix, progress_callback, cancel_event
            ):
                return False
            try:
                _verify_published_md5(dest + suffix, url + suffix)
            except (OSError, WGSExtractError) as e:
                logging.error(f"CADD {build} {remote_name}{suffix}: {e}")
                return False
    return True
