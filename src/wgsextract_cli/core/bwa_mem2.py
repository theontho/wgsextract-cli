"""bwa-mem2: BWA-MEM's alignments in about 3/4 of the time, for the price of a large index.

Its alignments are the same as bwa mem's, ALT-aware with the same <ref>.alt.
Mapping holds the whole index in memory (17 GB for GRCh38, against bwa's 5 GB),
and building that index takes 28 bytes of memory per reference base (~90 GB
for GRCh38), more than most computers have. For hs38DH, whose sequences match
Broad's Homo_sapiens_assembly38, nf-core's iGenomes publishes the built index.
"""

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import NamedTuple

import psutil

from wgsextract_cli.core.alignment_memory import (
    available_memory,
    estimate_mapping_memory,
    reference_bases,
)
from wgsextract_cli.core.paths import append_suffix
from wgsextract_cli.core.ref_library.downloads import (
    download_file,
    verify_download_sha256,
)
from wgsextract_cli.core.reference_assets import reference_dict_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

INDEX_SUFFIXES = (".0123", ".amb", ".ann", ".bwt.2bit.64", ".pac")
# bwa-mem2's documented indexing requirement; chr20 measured 24 bytes per base.
BUILD_BYTES_PER_BASE = 28


class PrebuiltIndex(NamedTuple):
    name: str
    base_url: str
    # suffix -> (size in bytes, SHA-256)
    files: dict[str, tuple[int, str]]


# Keyed by contig_fingerprint: the reference's contig names, lengths and MD5s.
PREBUILT_INDEXES = {
    "d9e37f99db847b3f5aa7a985195bc2faeca705e3195c00def061223ea2649935": PrebuiltIndex(
        "GRCh38 with ALT, decoy and HLA contigs (hs38DH)",
        "https://ngi-igenomes.s3.amazonaws.com/igenomes/Homo_sapiens/GATK/GRCh38/"
        "Sequence/BWAmem2Index/Homo_sapiens_assembly38.fasta",
        {
            ".0123": (
                6434693834,
                "5d9a5f465f9207fc2e08cfade640c55f024562d2c561acaa9c3af3228066a4aa",
            ),
            ".amb": (
                20199,
                "4199db9e913e372b2dae4ac17723def657f696daf71024b66f6b695947918c77",
            ),
            ".ann": (
                455474,
                "49231cd8462826afe424aeb20b6b2b9ecff32ff1a90f88f5d5f3c1a0cfb9ba99",
            ),
            ".bwt.2bit.64": (
                10456377594,
                "51698a28065c245dc400a272f0132c28669904548bd395f9ccb011c3072607a3",
            ),
            ".pac": (
                804336731,
                "5dd6629f95e9765a19821fd1ffc3e883e5af177527a220a6b72a4e5e492114dc",
            ),
        },
    ),
}


def has_index(ref: str) -> bool:
    return all(append_suffix(ref, s).is_file() for s in INDEX_SUFFIXES)


def _total_memory() -> int:
    return int(psutil.virtual_memory().total)


def mapping_memory(
    ref: str, *, threads: str = "1", memory: str = "512M", samblaster: bool = True
) -> int:
    return estimate_mapping_memory(
        ref, "bwa-mem2", threads=threads, memory=memory, samblaster=samblaster
    ).total


def build_memory(ref: str) -> int:
    return reference_bases(ref) * BUILD_BYTES_PER_BASE


def can_map(
    ref: str, *, threads: str = "1", memory: str = "512M", samblaster: bool = True
) -> bool:
    return available_memory() >= mapping_memory(
        ref, threads=threads, memory=memory, samblaster=samblaster
    )


def _gb(n: int) -> str:
    return f"{n / 1024**3:.0f} GB"


def contig_fingerprint(ref: str) -> str | None:
    """SHA-256 of the reference's contig names, lengths and sequence MD5s, in
    order, from its sequence dictionary (made with samtools dict if missing)."""
    dict_path = Path(reference_dict_path(ref))
    try:
        if dict_path.is_file():
            text = dict_path.read_text()
        else:
            text = run_command(["samtools", "dict", ref], capture_output=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"No sequence dictionary for {ref}: {e}")
        return None
    rows = []
    for line in text.splitlines():
        if not line.startswith("@SQ"):
            continue
        tags = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
        if "M5" not in tags:
            return None
        rows.append(f"{tags.get('SN')}\t{tags.get('LN')}\t{tags['M5']}\n")
    return hashlib.sha256("".join(rows).encode()).hexdigest() if rows else None


def prebuilt_index(ref: str) -> PrebuiltIndex | None:
    fingerprint = contig_fingerprint(ref)
    return PREBUILT_INDEXES.get(fingerprint) if fingerprint else None


def download_index(ref: str, prebuilt: PrebuiltIndex) -> None:
    total = sum(size for size, _ in prebuilt.files.values())
    logging.info(
        f"Downloading the bwa-mem2 index for {prebuilt.name} ({_gb(total)}) "
        f"next to {ref}..."
    )
    for suffix, (size, sha256) in prebuilt.files.items():
        dest = str(append_suffix(ref, suffix))
        if Path(dest).is_file() and Path(dest).stat().st_size == size:
            continue
        if not download_file(prebuilt.base_url + suffix, dest):
            raise WGSExtractError(
                f"Downloading {dest} failed; run the command again to resume."
            )
        if Path(dest).stat().st_size != size or not verify_download_sha256(
            dest, sha256
        ):
            Path(dest).unlink(missing_ok=True)
            raise WGSExtractError(f"{dest} didn't download intact; run it again.")


def build_index(ref: str) -> None:
    logging.info(
        f"Building the bwa-mem2 index for {ref} (uses up to "
        f"{_gb(build_memory(ref))} of memory; about an hour for a human genome)..."
    )
    try:
        run_command(["bwa-mem2", "index", ref])
    except (OSError, subprocess.SubprocessError) as e:
        for suffix in INDEX_SUFFIXES:
            append_suffix(ref, suffix).unlink(missing_ok=True)
        raise WGSExtractError(f"bwa-mem2 indexing failed: {e}") from e


def ensure_index(ref: str, *, download: bool) -> None:
    """Give the reference a bwa-mem2 index: download the published one when
    allowed and there is one, else build it if this computer has the memory."""
    if has_index(ref):
        return
    prebuilt = prebuilt_index(ref)
    if download and prebuilt:
        download_index(ref, prebuilt)
    elif _total_memory() >= build_memory(ref):
        build_index(ref)
    else:
        raise _unbuildable(ref, prebuilt)


def check_index(ref: str) -> None:
    """Fail before hours of other work when ensure_index(download=False) would."""
    if not has_index(ref) and _total_memory() < build_memory(ref):
        raise _unbuildable(ref, prebuilt_index(ref))


def _unbuildable(ref: str, prebuilt: PrebuiltIndex | None) -> WGSExtractError:
    how = (
        "Run `wgsextract ref index --aligner bwa-mem2` to download the published one"
        if prebuilt
        else "Build it on a computer with that much memory and copy the "
        f"{', '.join(INDEX_SUFFIXES)} files next to the reference"
    )
    return WGSExtractError(
        f"{ref} has no bwa-mem2 index, and building one needs "
        f"{_gb(build_memory(ref))} of memory (this computer has "
        f"{_gb(_total_memory())}). {how}, or align with --aligner bwa."
    )
