"""The gnomAD v4.1 allele-frequency table, streamed from its joint VCFs."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, urlopen

from wgsextract_cli.core.download_progress import (
    CancelEvent,
    ProgressCallback,
    format_bytes,
)
from wgsextract_cli.core.utils import (
    WGSExtractError,
    popen,
    run_command,
    wait_with_cancel,
)

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
            Path(partial).replace(part)
            return None
        err.seek(0)
        detail = err.read().decode(errors="replace").strip().splitlines()[-3:]
    Path(partial).unlink()
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
    target_dir = str(Path(reflib_dir, "ref"))
    dest = str(Path(target_dir, GNOMAD_AF_TABLE))
    if Path(dest).is_file() and Path(dest + ".tbi").is_file():
        logging.info(f"{GNOMAD_AF_TABLE} is already installed.")
        return True
    parts_dir = str(Path(target_dir, ".gnomad_hg38_parts"))
    Path(parts_dir).mkdir(parents=True, exist_ok=True)

    def part_path(contig: str) -> str:
        return str(Path(parts_dir, f"{contig}.tsv.gz"))

    urls = {c: GNOMAD_V4_URL.format(contig=c) for c in GNOMAD_V4_CONTIGS}
    try:
        sizes = {c: _remote_size(url) for c, url in urls.items()}
    except OSError as e:
        logging.error(f"Could not reach the gnomAD v4.1 bucket: {e}")
        return False
    total = sum(sizes.values())
    resumed = sum(sizes[c] for c in urls if Path(part_path(c)).is_file())
    todo = sorted(
        (c for c in urls if not Path(part_path(c)).is_file()),
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
    Path(partial).replace(dest)
    logging.info(f"Indexing {GNOMAD_AF_TABLE}...")
    try:
        run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", dest])
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        Path(dest).unlink()
        logging.error(f"Indexing {dest} failed: {e}")
        return False
    shutil.rmtree(parts_dir)
    return True
