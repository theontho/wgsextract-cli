from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any
from urllib.request import Request, urlopen

from wgsextract_cli.core.download_progress import (
    CancelEvent,
    ProgressCallback,
    format_bytes,
)
from wgsextract_cli.core.pharmgkb import download_pharmgkb
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command

from .ref_library import (
    download_file,
)

__all__ = ["CancelEvent", "ProgressCallback", "download_pharmgkb"]


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
    logging.info(f"Verifying MD5 of {os.path.basename(path)}...")
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        os.remove(path)
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
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)
    files = [
        (indel_name, f"cadd_indel_{build}.tsv.gz"),
        (CADD_SNV_FILE, f"cadd_{build}.tsv.gz"),
    ]
    for remote_name, local_name in files:
        dest = os.path.join(target_dir, local_name)
        if os.path.isfile(dest) and os.path.isfile(dest + ".tbi"):
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


# GERP++ rejected-substitution scores, one per base. Ensembl publishes the
# GRCh38 file (contigs 1, 2, ...); UCSC still serves the original hg19 one.
GERP_URLS = {
    "hg38": "https://ftp.ensembl.org/pub/release-114/compara/conservation_scores/"
    "91_mammals.gerp_conservation_score/"
    "gerp_conservation_scores.homo_sapiens.GRCh38.bw",
    "hg19": "https://hgdownload.soe.ucsc.edu/gbdb/hg19/bbi/All_hg19_RS.bw",
}


def open_bigwig(path: str) -> Any:
    """Open a bigWig with pyBigWig, raising WGSExtractError if it can't be read."""
    try:
        import pyBigWig
    except ImportError as e:
        raise WGSExtractError(
            "Reading GERP scores needs the pyBigWig Python package, which the "
            "default Pixi environment provides on macOS and Linux."
        ) from e
    try:
        bw = pyBigWig.open(path)
    except RuntimeError:
        bw = None
    if bw is None or not bw.isBigWig() or not bw.chroms():
        if bw is not None:
            bw.close()
        raise WGSExtractError(f"{path} is not a readable bigWig file.")
    return bw


def download_gerp(
    reflib_dir: str,
    build: str = "hg38",
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads the GERP++ RS bigWig for one build into <library>/ref."""
    target_dir = os.path.join(reflib_dir, "ref")
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, f"gerp_{build}.bw")
    if os.path.isfile(dest):
        logging.info(f"gerp_{build}.bw is already installed.")
        return True
    logging.info(f"Downloading GERP {build} scores...")
    if not download_file(GERP_URLS[build], dest, progress_callback, cancel_event):
        return False
    try:
        open_bigwig(dest).close()
    except WGSExtractError as e:
        os.remove(dest)
        logging.error(f"{e} It was removed; rerun to download it again.")
        return False
    return True


# gnomAD v4.1 joint (exomes + genomes) sites are GRCh38-only and ~900 GB of
# per-chromosome VCFs with ~660 INFO fields, so `ref gnomad --release 4.1`
# streams them into a small CHROM/POS/REF/ALT/AF table instead of storing them.
GNOMAD_V4_URL = (
    "https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/vcf/joint/"
    "gnomad.joint.v4.1.sites.{contig}.vcf.bgz"
)
GNOMAD_V4_CONTIGS = [f"chr{c}" for c in (*range(1, 23), "X", "Y")]
GNOMAD_AF_TABLE = "gnomad_hg38.tsv.gz"
# Sites gnomAD itself filtered out count as absent, so rare-variant filtering
# keeps the user's call rather than trusting an artifact's frequency.
_GNOMAD_V4_QUERY = (
    "-i",
    'FILTER="PASS" && AF_joint>0',
    "-f",
    "%CHROM\t%POS\t%REF\t%ALT\t%AF_joint\n",
)
_GNOMAD_V4_HEADER = (
    "## gnomAD v4.1 joint exomes+genomes, PASS sites with AF_joint > 0\n"
    "#CHROM\tPOS\tREF\tALT\tAF\n"
)


def _remote_size(url: str) -> int:
    with urlopen(Request(url, method="HEAD"), timeout=60) as response:
        return int(response.headers.get("Content-Length") or 0)


def _stream_gnomad_contig(
    url: str,
    part: str,
    cancel_event: CancelEvent | None,
    on_bytes: Callable[[int], None],
) -> str | None:
    """Write one contig's AF rows to part (BGZF); return an error message or None.

    The download is fed to bcftools here (not opened by htslib) so progress is
    exact, and so a cut-off download fails instead of leaving a short table.
    """
    partial = part + ".partial"
    download_error: list[str] = []
    with open(partial, "wb") as out, tempfile.TemporaryFile() as err:
        query = popen(
            ["bcftools", "query", *_GNOMAD_V4_QUERY, "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
        )
        compress = popen(["bgzip", "-c"], stdin=query.stdout, stdout=out)
        assert query.stdin is not None and query.stdout is not None
        query.stdout.close()
        sink = query.stdin

        def feed() -> None:
            try:
                with urlopen(url, timeout=120) as response:
                    while chunk := response.read(1 << 20):
                        sink.write(chunk)
                        on_bytes(len(chunk))
            except BrokenPipeError:
                pass  # bcftools exited; its status says why
            except (OSError, HTTPException) as e:
                download_error.append(f"download failed: {e}")
                query.kill()
            finally:
                try:
                    sink.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        queried = wait_with_cancel(query, cancel_event)
        feeder.join()
        compressed = compress.wait() == 0
        if queried and compressed and not download_error:
            os.replace(partial, part)
            return None
        err.seek(0)
        detail = err.read().decode(errors="replace").strip().splitlines()[-3:]
    os.remove(partial)
    return (
        "; ".join(download_error + detail) or f"bcftools exited with {query.returncode}"
    )


def build_gnomad_af(
    reflib_dir: str,
    workers: int = 6,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
    attempts: int = 3,
) -> bool:
    """
    Build <library>/ref/gnomad_hg38.tsv.gz from the gnomAD v4.1 joint VCFs.

    Contigs stream in parallel (the build is network-bound) into resumable
    per-contig parts, which are then concatenated (BGZF allows that) and indexed.
    """
    target_dir = os.path.join(reflib_dir, "ref")
    dest = os.path.join(target_dir, GNOMAD_AF_TABLE)
    if os.path.isfile(dest) and os.path.isfile(dest + ".tbi"):
        logging.info(f"{GNOMAD_AF_TABLE} is already installed.")
        return True
    parts_dir = os.path.join(target_dir, ".gnomad_hg38_parts")
    os.makedirs(parts_dir, exist_ok=True)

    def part_path(contig: str) -> str:
        return os.path.join(parts_dir, f"{contig}.tsv.gz")

    urls = {c: GNOMAD_V4_URL.format(contig=c) for c in GNOMAD_V4_CONTIGS}
    try:
        sizes = {c: _remote_size(url) for c, url in urls.items()}
    except OSError as e:
        logging.error(f"Could not reach the gnomAD v4.1 bucket: {e}")
        return False
    total = sum(sizes.values())
    resumed = sum(sizes[c] for c in urls if os.path.isfile(part_path(c)))
    todo = sorted(
        (c for c in urls if not os.path.isfile(part_path(c))),
        key=sizes.__getitem__,
        reverse=True,  # biggest first, so the last contig to finish is short
    )
    logging.info(
        f"Streaming {len(todo)} gnomAD v4.1 contigs ({format_bytes(total - resumed)} "
        f"of VCF, {workers} at a time) into an allele-frequency table..."
    )

    progress_lock = threading.Lock()
    streamed = dict.fromkeys(todo, 0)

    def stream(contig: str) -> str | None:
        def count(n: int) -> None:
            with progress_lock:
                streamed[contig] += n

        error = None
        for attempt in range(1, attempts + 1):
            if cancel_event and cancel_event.is_set():
                return "cancelled"
            with progress_lock:
                streamed[contig] = 0
            error = _stream_gnomad_contig(
                urls[contig], part_path(contig), cancel_event, count
            )
            if error is None:
                return None
            logging.warning(f"gnomAD {contig} attempt {attempt} failed: {error}")
        return error

    def report(note: str) -> None:
        with progress_lock:
            done = resumed + sum(streamed.values())
        rate = (done - resumed) / max(time.monotonic() - start, 1e-9)
        eta = f", about {(total - done) / rate / 60:.0f} min left" if rate else ""
        logging.info(
            f"gnomAD {note}{format_bytes(done)} of {format_bytes(total)} "
            f"at {format_bytes(rate)}/s{eta}"
        )
        if progress_callback:
            progress_callback(done, total, rate)

    start = time.monotonic()
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = {pool.submit(stream, c): c for c in todo}
        while pending:
            finished, _ = wait(pending, timeout=60, return_when=FIRST_COMPLETED)
            if not finished:
                report("")
            for future in finished:
                contig = pending.pop(future)
                if future.result() is None:
                    report(f"{contig} done: ")
                else:
                    failed.append(contig)
    if cancel_event and cancel_event.is_set():
        return False
    if failed:
        logging.error(
            f"gnomAD contigs failed: {', '.join(failed)}. Rerun to resume; "
            "finished contigs are kept."
        )
        return False

    partial = dest + ".partial"
    with open(partial, "wb") as out:
        header = popen(["bgzip", "-c"], stdin=subprocess.PIPE, stdout=out)
        header.communicate(_GNOMAD_V4_HEADER.encode())
        if header.returncode != 0:
            raise WGSExtractError("bgzip failed to write the gnomAD table header.")
        out.flush()
        for contig in GNOMAD_V4_CONTIGS:
            with open(part_path(contig), "rb") as part:
                shutil.copyfileobj(part, out, 8 << 20)
    os.replace(partial, dest)
    logging.info(f"Indexing {GNOMAD_AF_TABLE}...")
    try:
        run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", dest])
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        os.remove(dest)
        logging.error(f"Indexing {dest} failed: {e}")
        return False
    shutil.rmtree(parts_dir)
    return True


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
