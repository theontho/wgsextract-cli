"""BGZF: the blocked gzip that htslib needs for random access."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

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

    # By content, not name: some mirrors serve plain FASTA as .fa.gz. gzip -dc
    # also reads a single-member zip (YSEQ's hg19.zip).
    with open(path, "rb") as f:
        is_gz = f.read(2) in (b"\x1f\x8b", b"PK")
    new_path = path if path.endswith(".gz") else path + ".gz"
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
        Path(tmp_path).replace(new_path)
        if new_path != path:
            Path(path).unlink()
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
        if Path(tmp_path).exists():
            Path(tmp_path).unlink()
    logging.info(f"Recompression to {new_path} complete.")
    return new_path
