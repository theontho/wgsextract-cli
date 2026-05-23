from __future__ import annotations

import getpass
import hashlib
import logging
import os
import platform
import re
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_ENV_VAR = "WGSEXTRACT_DEV_DOWNLOAD_CACHE"
CACHE_TTL_ENV_VAR = "WGSEXTRACT_DEV_DOWNLOAD_CACHE_TTL_SECONDS"


@dataclass(frozen=True)
class DevCacheHint:
    system: str | None = None
    username: str | None = None
    computer_name: str | None = None

    def matches(self) -> bool:
        if self.system and platform.system().lower() != self.system.lower():
            return False
        if self.username and _current_username().lower() != self.username.lower():
            return False
        if (
            self.computer_name
            and _current_computer_name().lower() != self.computer_name.lower()
        ):
            return False
        return True


DEV_CACHE_HINTS: tuple[DevCacheHint, ...] = (
    DevCacheHint(system="Darwin", username="mac"),
    DevCacheHint(system="Windows", computer_name="minipc"),
)


def dev_download_cache_enabled() -> bool:
    override = os.environ.get(CACHE_ENV_VAR)
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return any(hint.matches() for hint in DEV_CACHE_HINTS)


def xdg_cache_home() -> Path:
    value = os.environ.get("XDG_CACHE_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".cache"


def wgsextract_cache_root() -> Path:
    return xdg_cache_home() / "wgsextract"


def download_cache_root() -> Path:
    return wgsextract_cache_root() / "downloads"


def benchmark_dataset_cache_root() -> Path:
    return wgsextract_cache_root() / "benchmark-datasets"


def restore_cached_download(
    url: str,
    destination: Path,
    *,
    checksum_hint: str | None = None,
) -> Path | None:
    if not dev_download_cache_enabled():
        return None

    root = download_cache_root()
    prune_expired_cache_items(root)
    cached_path = cached_download_path(url, destination, checksum_hint=checksum_hint)
    if not cached_path.is_file() or cache_item_is_expired(cached_path):
        return None

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_name(f".{destination.name}.cache-{os.getpid()}.tmp")
        shutil.copyfile(cached_path, tmp_path)
        tmp_path.replace(destination)
        mark_cache_item_used(cached_path)
        logging.info("Using dev download cache: %s", cached_path)
        return cached_path
    except OSError as exc:
        logging.debug(
            "Could not restore dev download cache item %s: %s", cached_path, exc
        )
        return None


def store_download_in_dev_cache(
    url: str,
    source: Path,
    *,
    checksum_hint: str | None = None,
) -> Path | None:
    if not dev_download_cache_enabled() or not source.is_file():
        return None

    root = download_cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.debug("Could not create dev download cache %s: %s", root, exc)
        return None

    prune_expired_cache_items(root)
    cached_path = cached_download_path(url, source, checksum_hint=checksum_hint)
    try:
        if cached_path.exists() and source.resolve() == cached_path.resolve():
            mark_cache_item_used(cached_path)
            return cached_path

        tmp_path = cached_path.with_name(f".{cached_path.name}.{os.getpid()}.tmp")
        shutil.copyfile(source, tmp_path)
        tmp_path.replace(cached_path)
        mark_cache_item_used(cached_path)
        logging.info("Stored dev download cache item: %s", cached_path)
        return cached_path
    except OSError as exc:
        logging.debug(
            "Could not store dev download cache item %s: %s", cached_path, exc
        )
        return None


def drop_cached_download(
    url: str,
    destination: Path,
    *,
    checksum_hint: str | None = None,
) -> None:
    cached_path = cached_download_path(url, destination, checksum_hint=checksum_hint)
    try:
        cached_path.unlink(missing_ok=True)
    except OSError as exc:
        logging.debug(
            "Could not remove dev download cache item %s: %s", cached_path, exc
        )


def cached_download_path(
    url: str,
    destination: Path,
    *,
    checksum_hint: str | None = None,
) -> Path:
    key = hashlib.sha256(f"{url}\0{checksum_hint or ''}".encode()).hexdigest()
    return download_cache_root() / f"{key[:16]}-{_cache_filename(url, destination)}"


def cache_item_is_expired(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > _cache_ttl_seconds()
    except OSError:
        return True


def mark_cache_item_used(path: Path) -> None:
    try:
        if path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            marker = path / ".dev-cache-touch"
            marker.touch()
            os.utime(path, None)
        else:
            os.utime(path, None)
    except OSError as exc:
        logging.debug("Could not touch dev cache item %s: %s", path, exc)


def prune_expired_cache_items(root: Path) -> None:
    if not root.exists():
        return
    now = time.time()
    ttl = _cache_ttl_seconds()
    try:
        items = list(root.iterdir())
    except OSError as exc:
        logging.debug("Could not list dev cache root %s: %s", root, exc)
        return
    for item in items:
        try:
            if now - item.stat().st_mtime <= ttl:
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError as exc:
            logging.debug("Could not prune dev cache item %s: %s", item, exc)


def _cache_filename(url: str, destination: Path) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name or destination.name or "download"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or "download"


def _cache_ttl_seconds() -> int:
    value = os.environ.get(CACHE_TTL_ENV_VAR)
    if not value:
        return CACHE_TTL_SECONDS
    try:
        ttl = int(value)
    except ValueError:
        logging.debug("Ignoring invalid %s=%r", CACHE_TTL_ENV_VAR, value)
        return CACHE_TTL_SECONDS
    return max(0, ttl)


def _current_username() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or getpass.getuser()


def _current_computer_name() -> str:
    return (
        os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or platform.node()
        or socket.gethostname()
    )
