from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Literal, Protocol
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from wgsextract_cli.core.dev_download_cache import (
    drop_cached_download,
    restore_cached_download,
    store_download_in_dev_cache,
)
from wgsextract_cli.core.download_progress import (
    DownloadCancelled,
    copy_response_to_file,
    curl_progress_args,
    require_http_url,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command


class CancelEvent(Protocol):
    def is_set(self) -> bool: ...


ProgressCallback = Callable[[int, int, float], None]


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
        logging.info(f"Verified GitHub release asset SHA256: {actual_sha256}")
        return True

    logging.error("Downloaded file checksum mismatch.")
    logging.error(f"Expected SHA256: {expected_sha256.lower()}")
    logging.error(f"Actual SHA256:   {actual_sha256}")
    try:
        os.remove(path)
    except OSError:
        pass
    return False


def download_file(
    url: str,
    dest: str,
    progress_callback: ProgressCallback | None = None,
    cancel_event: CancelEvent | None = None,
) -> bool:
    """Downloads a file with progress reporting, optional cancellation, and resume support."""
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

    # Use curl only when it can surface its native progress bar directly.
    # Non-TTY runs should take the urllib path so progress is emitted as logs.
    curl_args = curl_progress_args()
    if (
        progress_callback is None
        and cancel_event is None
        and "--progress-bar" in curl_args
    ):
        try:
            # Use -L to follow redirects, -C - for resume
            cmd = ["curl", "-L", *curl_args]
            if os.path.exists(dest):
                cmd.extend(["-C", "-"])
            cmd.extend(["-o", dest, url])

            run_command(cmd, capture_output=False)
            verified = verify_download_sha256(dest, expected_sha256)
            if verified:
                store_download_in_dev_cache(url, dest_path, checksum_hint=checksum_hint)
            return verified
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("curl download failed, falling back to urllib: %s", e)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    initial_size = 0
    mode: Literal["ab", "wb"] = "wb"

    # If the final file exists but we are here, it might be incomplete (e.g. missing index)
    # Move it to .partial to attempt a resume/verify
    if not os.path.exists(partial_dest) and os.path.exists(dest):
        os.rename(dest, partial_dest)

    if os.path.exists(partial_dest):
        initial_size = os.path.getsize(partial_dest)
        if initial_size > 0:
            headers["Range"] = f"bytes={initial_size}-"
            mode = "ab"
        else:
            mode = "wb"

    try:
        req = Request(url, headers=headers)
        with urlopen(req) as response:
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
                    progress_label=os.path.basename(dest),
                    cancel_event=cancel_event,
                )

            with open(partial_dest, mode) as f:
                write_partial(f)

        # Rename to final destination on success
        if os.path.exists(dest):
            os.remove(dest)
        os.rename(partial_dest, dest)
        verified = verify_download_sha256(dest, expected_sha256)
        if verified:
            store_download_in_dev_cache(url, dest_path, checksum_hint=checksum_hint)
        return verified
    except DownloadCancelled as e:
        logging.info(str(e))
    except (
        OSError,
        subprocess.SubprocessError,
        WGSExtractError,
    ) as e:
        logging.error(f"Download error: {e}")
    return False
