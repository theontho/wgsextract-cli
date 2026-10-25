"""BGZF: the blocked gzip that htslib needs for random access."""

from __future__ import annotations

import gzip
import logging
import subprocess
import threading
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import IO

from wgsextract_cli.core.download_progress import CancelEvent
from wgsextract_cli.core.utils import WGSExtractError, popen, wait_with_cancel


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


def open_decompressed(path: str) -> gzip.GzipFile | IO[bytes]:
    """A gzip file's content (every member), or a single-file zip's."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"PK":
        archive = zipfile.ZipFile(path)
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1:
            archive.close()
            raise WGSExtractError(
                f"{path} is a zip of {len(members)} files; expected one FASTA."
            )
        return archive.open(members[0])
    return gzip.open(path, "rb")


def _feed_decompressed(
    path: str,
    proc: subprocess.Popen[bytes],
    errors: list[BaseException],
) -> None:
    """Write path's decompressed content to proc's stdin (run in a thread)."""
    assert proc.stdin is not None
    try:
        with open_decompressed(path) as source:
            while chunk := source.read(1 << 20):
                proc.stdin.write(chunk)
    except BrokenPipeError:
        pass  # bgzip stopped: cancelled, or its exit status says why
    except (
        OSError,
        EOFError,
        RuntimeError,
        zlib.error,
        zipfile.BadZipFile,
        WGSExtractError,
    ) as e:
        errors.append(e)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


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

    # By content, not name: some mirrors serve plain FASTA as .fa.gz, and YSEQ
    # serves hg19 as a single-file zip.
    with open(path, "rb") as f:
        magic = f.read(2)
    compressed = magic in (b"\x1f\x8b", b"PK")
    new_path = path if path.endswith(".gz") else path + ".gz"
    tmp_path = path + ".tmp.gz"
    proc: subprocess.Popen[bytes] | None = None
    feed_errors: list[BaseException] = []
    try:
        with open(tmp_path, "wb") as f_out:
            if compressed:
                # Decompress here, not with gzip(1): Windows has no native gzip.
                proc = popen(["bgzip", "-c"], stdin=subprocess.PIPE, stdout=f_out)
                feeder = threading.Thread(
                    target=_feed_decompressed,
                    args=(path, proc, feed_errors),
                    daemon=True,
                )
                feeder.start()
            else:
                proc = popen(["bgzip", "-c", path], stdout=f_out)
            wait_with_cancel(proc, cancel_event)
            if compressed:
                feeder.join()
            if cancel_event and cancel_event.is_set():
                return None
            if feed_errors:
                raise WGSExtractError(
                    f"Could not decompress {path}: {feed_errors[0]}"
                ) from feed_errors[0]
            if proc.wait() != 0:
                raise WGSExtractError(
                    f"BGZF recompression of {path} failed (bgzip exit {proc.wait()})."
                )
        Path(tmp_path).replace(new_path)
        if new_path != path:
            Path(path).unlink()
    except OSError as e:
        raise WGSExtractError(f"BGZF recompression of {path} failed: {e}") from e
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
    logging.info(f"Recompression to {new_path} complete.")
    return new_path
