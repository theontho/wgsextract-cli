from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from wgsextract_cli.core.dev_download_cache import (
    drop_cached_download,
    restore_cached_download,
    store_download_in_dev_cache,
)
from wgsextract_cli.core.download_progress import (
    DownloadCancelled,
    DownloadIncomplete,
    copy_response_to_file,
    curl_progress_args,
    require_http_url,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command

from . import segmented


class CancelEvent(Protocol):
    def is_set(self) -> bool: ...


ProgressCallback = Callable[[int, int, float], None]

# Socket timeout (seconds) for stalled connections; not a cap on total download time.
DOWNLOAD_TIMEOUT_SECONDS = 60
# Attempts at one URL: an interrupted transfer resumes where it stopped.
DOWNLOAD_ATTEMPTS = 5
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)
# Failures that leave a usable .partial behind.
_RESUMABLE = (DownloadIncomplete, IncompleteRead, TimeoutError, ConnectionError)


def resolve_github_release_asset_sha256(url: str) -> str | None:
    """Return GitHub's sha256 digest for a release asset URL, if applicable."""
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None

    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) == 6 and parts[2:4] == ["releases", "download"]:
        owner, repo, tag, asset_name = parts[0], parts[1], parts[4], parts[5]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    elif len(parts) == 6 and parts[2:5] == ["releases", "latest", "download"]:
        owner, repo, asset_name = parts[0], parts[1], parts[5]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    else:
        return None

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "wgsextract-cli",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    req = Request(api_url, headers=headers)
    with urlopen(req, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))

    for asset in release.get("assets", []):
        if asset.get("name") != asset_name:
            continue
        digest = str(asset.get("digest", ""))
        match = re.fullmatch(r"(?i)sha256:([a-f0-9]{64})", digest)
        if not match:
            raise ValueError(
                f"GitHub release asset {asset_name} did not include a sha256 digest."
            )
        return match.group(1).lower()

    raise ValueError(
        f"GitHub release asset metadata was not found for {asset_name} at {api_url}."
    )


def verify_download_sha256(path: str, expected_sha256: str | None) -> bool:
    """Verify a downloaded file against an expected SHA-256 digest."""
    if not expected_sha256:
        return True

    sha256 = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
    except OSError as e:
        logging.error(f"Could not read downloaded file for checksum verification: {e}")
        return False

    actual_sha256 = sha256.hexdigest()
    if actual_sha256 == expected_sha256.lower():
        logging.info(f"Verified SHA-256 of {Path(path).name}: {actual_sha256}")
        return True

    logging.error("Downloaded file checksum mismatch.")
    logging.error(f"Expected SHA256: {expected_sha256.lower()}")
    logging.error(f"Actual SHA256:   {actual_sha256}")
    try:
        Path(path).unlink()
    except OSError:
        pass
    return False


def download_file(
    url: str,
    dest: str,
    progress_callback: ProgressCallback | None = None,
    cancel_event: CancelEvent | None = None,
) -> bool:
    """Resume explicit .partial files; replace dest only after verification."""
    require_http_url(url, "download URL")
    partial_dest = dest + ".partial"
    try:
        expected_sha256 = resolve_github_release_asset_sha256(url)
    except OSError as e:
        logging.warning(
            "Could not resolve GitHub release asset checksum for %s: %s. "
            "Continuing without GitHub asset SHA-256 verification.",
            url,
            e,
        )
        expected_sha256 = None
    except ValueError as e:
        logging.error(
            "Could not resolve GitHub release asset checksum for %s: %s", url, e
        )
        return False

    checksum_hint = f"sha256:{expected_sha256}" if expected_sha256 else None
    dest_path = Path(dest)
    partial_path = Path(partial_dest)
    if not dest_path.exists() and not partial_path.exists():
        if restore_cached_download(url, dest_path, checksum_hint=checksum_hint):
            if verify_download_sha256(dest, expected_sha256):
                return True
            drop_cached_download(url, dest_path, checksum_hint=checksum_hint)
            dest_path.unlink(missing_ok=True)

    # A large file over several connections (servers that cap each one), unless
    # an earlier single-connection attempt left a partial to resume.
    segments_path = Path(partial_dest + ".segments")
    single_partial = partial_path.exists() and not segments_path.exists()
    if not single_partial:
        size = segmented.ranged_size(url)
        if size is None and segments_path.exists():
            # A started segmented download is finished as one: its partial is
            # full-size with holes, which one connection would take as done.
            size = segmented.saved_size(str(segments_path), url)
        if size is None and segments_path.exists():
            logging.info("Unfinished download has no usable state; starting over.")
            partial_path.unlink(missing_ok=True)
            segments_path.unlink(missing_ok=True)
        elif size is not None and (
            size >= segmented.SEGMENTED_MIN_SIZE or segments_path.exists()
        ):
            try:
                verified = segmented.download_segmented(
                    url,
                    dest,
                    size,
                    progress_callback,
                    cancel_event,
                    verify=lambda path: verify_download_sha256(path, expected_sha256),
                )
            except segmented.RangesUnsupported:
                logging.info("Server ignores byte ranges; using one connection.")
                for leftover in (partial_dest, partial_dest + ".segments"):
                    Path(leftover).unlink(missing_ok=True)
            except DownloadCancelled as e:
                logging.info(str(e))
                return False
            except (OSError, HTTPException) as e:
                logging.error(f"Download error: {e} (run the command again to resume)")
                return False
            else:
                if verified:
                    store_download_in_dev_cache(
                        url, dest_path, checksum_hint=checksum_hint
                    )
                return verified

    # Use curl only when it can surface its native progress bar directly.
    # Non-TTY runs should take the urllib path so progress is emitted as logs.
    curl_args = curl_progress_args()
    if (
        progress_callback is None
        and cancel_event is None
        and "--progress-bar" in curl_args
    ):
        try:
            # Into dest.partial, so an interrupted curl never leaves a short
            # file at dest. -L follows redirects, --fail keeps HTTP error pages
            # out of the file, -C - resumes.
            cmd = ["curl", "-L", "--fail", *curl_args]
            if partial_path.exists():
                cmd.extend(["-C", "-"])
            cmd.extend(["-o", partial_dest, url])

            run_command(cmd, capture_output=False)
            return _finish_download(
                url, dest, partial_dest, expected_sha256, checksum_hint
            )
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("curl download failed, falling back to urllib: %s", e)

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _urllib_download(
                url,
                dest,
                progress_callback,
                cancel_event,
                expected_sha256,
                checksum_hint,
            )
        except _RESUMABLE as e:
            if attempt == DOWNLOAD_ATTEMPTS:
                logging.error(
                    f"Download error: {e} (gave up after {attempt} attempts; "
                    "run the command again to resume)"
                )
                return False
            logging.warning(
                f"Download interrupted ({e}); resuming "
                f"(attempt {attempt + 1} of {DOWNLOAD_ATTEMPTS})..."
            )
            time.sleep(min(30, 2**attempt))
    return False


def _urllib_download(
    url: str,
    dest: str,
    progress_callback: ProgressCallback | None,
    cancel_event: CancelEvent | None,
    expected_sha256: str | None,
    checksum_hint: str | None,
) -> bool:
    """One attempt; an interrupted transfer raises one of _RESUMABLE and leaves
    dest.partial for the next attempt to resume."""
    partial_dest = dest + ".partial"
    headers = {"User-Agent": _USER_AGENT}
    initial_size = 0
    mode: Literal["ab", "wb"] = "wb"

    if Path(partial_dest).exists():
        initial_size = Path(partial_dest).stat().st_size
        if initial_size > 0:
            headers["Range"] = f"bytes={initial_size}-"
            mode = "ab"
        else:
            mode = "wb"

    try:
        req = Request(url, headers=headers)
        try:
            response_cm = urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except HTTPError as e:
            if e.code != 416 or initial_size == 0:
                raise
            e.close()
            if not _partial_is_complete(
                url, e.headers.get("Content-Range"), initial_size
            ):
                # The partial file does not fit the remote file; start over.
                Path(partial_dest).unlink()
                return _urllib_download(
                    url,
                    dest,
                    progress_callback,
                    cancel_event,
                    expected_sha256,
                    checksum_hint,
                )
            logging.info("%s is already fully downloaded.", Path(dest).name)
            return _finish_download(
                url, dest, partial_dest, expected_sha256, checksum_hint
            )
        with response_cm as response:
            code = response.getcode()
            # If we requested a range but got 200, the server doesn't support range or sent full file
            if initial_size > 0 and code == 200:
                logging.info(
                    "Server does not support Range requests, starting from scratch."
                )
                initial_size = 0
                mode = "wb"

            def write_partial(f: BinaryIO) -> None:
                copy_response_to_file(
                    response,
                    f,
                    initial_size=initial_size,
                    progress_callback=progress_callback,
                    progress_label=Path(dest).name,
                    cancel_event=cancel_event,
                )

            with open(partial_dest, mode) as f:
                write_partial(f)

        return _finish_download(url, dest, partial_dest, expected_sha256, checksum_hint)
    except DownloadCancelled as e:
        logging.info(str(e))
    except _RESUMABLE:
        raise
    except URLError as e:
        # A server error (5xx) or no connection at all (DNS, refused, network
        # down) may pass; retry those. A client error (4xx) won't.
        if not isinstance(e, HTTPError) or e.code >= 500:
            raise ConnectionError(str(e)) from e
        logging.error(f"Download error: {e}")
    except (
        OSError,
        subprocess.SubprocessError,
        WGSExtractError,
    ) as e:
        logging.error(f"Download error: {e}")
    return False


def _partial_is_complete(
    url: str, content_range: str | None, partial_size: int
) -> bool:
    """A 416 reply to `Range: bytes=N-` means done when the remote size is N.

    The size is in the reply's Content-Range; servers that leave it out (such
    as Ensembl's) are asked with a HEAD request.
    """
    match = re.fullmatch(r"bytes \*/(\d+)", (content_range or "").strip())
    if match:
        return int(match.group(1)) == partial_size
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": _USER_AGENT})
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
    except (OSError, HTTPException):
        return False
    return length is not None and length.isdigit() and int(length) == partial_size


def _finish_download(
    url: str,
    dest: str,
    partial_dest: str,
    expected_sha256: str | None,
    checksum_hint: str | None,
) -> bool:
    if not verify_download_sha256(partial_dest, expected_sha256):
        return False
    Path(partial_dest).replace(dest)
    store_download_in_dev_cache(url, Path(dest), checksum_hint=checksum_hint)
    return True
