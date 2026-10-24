"""Per-base conservation scores (GERP++, PhyloP) as bigWig files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from wgsextract_cli.core.download_progress import CancelEvent, ProgressCallback
from wgsextract_cli.core.utils import WGSExtractError

from .ref_library import download_file

# UCSC's PhyloP 100-way vertebrate scores, one per base (~10 GB bigWigs). The
# Annovar PhyloP tables we used to fetch are gone (404).
PHYLOP_URLS = {
    "hg38": "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/phyloP100way/"
    "hg38.phyloP100way.bw",
    "hg19": "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/phyloP100way/"
    "hg19.100way.phyloP100way.bw",
}


# GERP++ rejected-substitution scores, one per base. Ensembl publishes the
# GRCh38 file (contigs 1, 2, ...); UCSC still serves the original hg19 one.
GERP_URLS = {
    "hg38": "https://ftp.ensembl.org/pub/release-114/compara/conservation_scores/"
    "91_mammals.gerp_conservation_score/"
    "gerp_conservation_scores.homo_sapiens.GRCh38.bw",
    "hg19": "https://hgdownload.soe.ucsc.edu/gbdb/hg19/bbi/All_hg19_RS.bw",
}


def _pybigwig() -> Any:
    try:
        import pyBigWig
    except ImportError as e:
        raise WGSExtractError(
            "Reading GERP and PhyloP scores needs the pyBigWig Python package, which the "
            "default Pixi environment provides on macOS and Linux."
        ) from e
    return pyBigWig


def open_bigwig(path: str) -> Any:
    """Open a bigWig with pyBigWig, raising WGSExtractError if it can't be read."""
    pyBigWig = _pybigwig()
    try:
        bw = pyBigWig.open(path)
    except RuntimeError:
        bw = None
    if bw is None or not bw.isBigWig() or not bw.chroms():
        if bw is not None:
            bw.close()
        raise WGSExtractError(f"{path} is not a readable bigWig file.")
    return bw


def _download_bigwig(
    reflib_dir: str,
    url: str,
    name: str,
    label: str,
    cancel_event: CancelEvent | None,
    progress_callback: ProgressCallback | None,
) -> bool:
    """Downloads a bigWig into <library>/ref/<name> and checks it opens."""
    target_dir = str(Path(reflib_dir, "ref"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    dest = str(Path(target_dir, name))
    if Path(dest).is_file():
        logging.info(f"{name} is already installed.")
        return True
    # Without pyBigWig the ~10 GB download couldn't be checked or used.
    _pybigwig()
    logging.info(f"Downloading {label} scores...")
    if not download_file(url, dest, progress_callback, cancel_event):
        return False
    try:
        open_bigwig(dest).close()
    except WGSExtractError as e:
        Path(dest).unlink()
        logging.error(f"{e} It was removed; rerun to download it again.")
        return False
    return True


def download_gerp(
    reflib_dir: str,
    build: str = "hg38",
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads the GERP++ RS bigWig for one build into <library>/ref."""
    return _download_bigwig(
        reflib_dir,
        GERP_URLS[build],
        f"gerp_{build}.bw",
        f"GERP {build}",
        cancel_event,
        progress_callback,
    )


def download_phylop(
    reflib_dir: str,
    build: str = "hg38",
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads the PhyloP 100-way bigWig for one build into <library>/ref."""
    return _download_bigwig(
        reflib_dir,
        PHYLOP_URLS[build],
        f"phylop_{build}.bw",
        f"PhyloP {build}",
        cancel_event,
        progress_callback,
    )
