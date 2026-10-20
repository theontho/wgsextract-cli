"""Large downloads over several connections.

Some servers cap each connection: Ensembl serves its 27.6 GB VEP cache at about
0.4 MB/s per connection, 19 hours over one. A file of at least
SEGMENTED_MIN_SIZE whose server takes byte ranges is fetched in SEGMENT_SIZE
pieces, CONNECTIONS at a time, straight into dest.partial. dest.partial.segments
records how much of each piece has arrived, so an interrupted download resumes
every piece where it stopped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from http.client import HTTPException, IncompleteRead
from urllib.request import Request, urlopen

from wgsextract_cli.core.download_progress import (
    CancelEvent,
    DownloadCancelled,
    DownloadIncomplete,
    PercentProgressLogger,
    ProgressCallback,
)

CONNECTIONS = 8
SEGMENTED_MIN_SIZE = 1024**3
SEGMENT_SIZE = 256 * 1024**2
ATTEMPTS = 5
TIMEOUT_SECONDS = 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)
_CHUNK = 1024**2
_RESUMABLE = (DownloadIncomplete, IncompleteRead, TimeoutError, ConnectionError)


class RangesUnsupported(Exception):
    """The server answered a byte-range request with the whole file."""


def ranged_size(url: str) -> int | None:
    """The file's size, if its server says it takes byte ranges."""
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            headers = response.headers
    except (OSError, HTTPException):
        return None
    length = headers.get("Content-Length") or ""
    if headers.get("Accept-Ranges", "").lower() != "bytes" or not length.isdigit():
        return None
    return int(length)


def _load_state(path: str, url: str, size: int) -> dict[str, int] | None:
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if state.get("url") != url or state.get("size") != size:
        return None
    return {str(k): int(v) for k, v in state.get("received", {}).items()}


def download_segmented(
    url: str,
    dest: str,
    size: int,
    progress_callback: ProgressCallback | None = None,
    cancel_event: CancelEvent | None = None,
) -> bool:
    """Fetch url (size bytes) into dest; False leaves what arrived for a rerun.

    Raises RangesUnsupported if the server ignores byte ranges after all.
    """
    partial = dest + ".partial"
    state_path = partial + ".segments"
    starts = list(range(0, size, SEGMENT_SIZE))
    received = _load_state(state_path, url, size) if os.path.exists(partial) else None
    if received is None:
        with open(partial, "wb") as f:
            f.truncate(size)
        received = {str(start): 0 for start in starts}
    lock = threading.Lock()
    stop = threading.Event()

    def save_state() -> None:
        with lock:
            snapshot = dict(received)
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"url": url, "size": size, "received": snapshot}, f)
        os.replace(tmp, state_path)

    def fetch(start: int) -> None:
        end = min(start + SEGMENT_SIZE, size)
        for attempt in range(1, ATTEMPTS + 1):
            pos = start + received[str(start)]
            if pos >= end:
                return
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Range": f"bytes={pos}-{end - 1}",
                    },
                )
                with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    if response.getcode() != 206:
                        raise RangesUnsupported(url)
                    with open(partial, "r+b") as f:
                        f.seek(pos)
                        while pos < end:
                            if stop.is_set():
                                return
                            chunk = response.read(min(_CHUNK, end - pos))
                            if not chunk:
                                raise DownloadIncomplete(
                                    f"bytes {pos}-{end - 1}: connection closed"
                                )
                            f.write(chunk)
                            pos += len(chunk)
                            with lock:
                                received[str(start)] = pos - start
                return
            except _RESUMABLE as e:
                if attempt == ATTEMPTS:
                    raise
                logging.debug(f"Retrying {url} bytes {pos}-{end - 1}: {e}")
                time.sleep(min(30, 2**attempt))

    label = os.path.basename(dest)
    report = progress_callback or PercentProgressLogger(label)
    logging.info(f"Downloading {label} over {CONNECTIONS} connections...")
    start_time = time.monotonic()
    already = sum(received.values())
    with ThreadPoolExecutor(CONNECTIONS) as pool:
        futures: set[Future[None]] = {
            pool.submit(fetch, start)
            for start in starts
            if received[str(start)] < min(SEGMENT_SIZE, size - start)
        }
        try:
            pending = futures
            while pending:
                done, pending = wait(pending, timeout=2, return_when=FIRST_EXCEPTION)
                save_state()
                for future in done:
                    future.result()  # raises the piece's error
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled("Download cancelled by user.")
                with lock:
                    total = sum(received.values())
                elapsed = time.monotonic() - start_time
                report(total, size, (total - already) / elapsed if elapsed else 0.0)
        except BaseException:
            stop.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            save_state()

    if sum(received.values()) != size:
        raise DownloadIncomplete(f"{label}: {sum(received.values())} of {size} bytes")
    os.replace(partial, dest)
    os.remove(state_path)
    return True
