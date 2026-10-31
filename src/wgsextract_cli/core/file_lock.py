"""Cooperating builders serialize publication with an exclusive lock file."""

import contextlib
import errno
import json
import logging
import os
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil

from wgsextract_cli.core.utils import WGSExtractError

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextlib.contextmanager
def _reclaim_guard(path: Path, deadline: float) -> Iterator[None]:
    # Keep this inode: deleting it would let concurrent reclaimers lock different files.
    fd = os.open(str(path) + ".guard", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                if time.monotonic() >= deadline:
                    raise WGSExtractError(
                        f"Timed out waiting for index builder lock: {path}"
                    ) from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _abandoned(path: Path, timeout: float) -> bool:
    try:
        record = json.loads(path.read_text())
        if record["host"] != socket.gethostname():
            return False
        try:
            process = psutil.Process(record["pid"])
            return bool(process.create_time() != record["created"])
        except psutil.NoSuchProcess:
            return True
        except psutil.AccessDenied:
            return False
    except (ValueError, KeyError, TypeError):
        return time.time() - path.stat().st_mtime > timeout


@contextlib.contextmanager
def build_lock(lock: str, timeout: float = 1800) -> Iterator[None]:
    """Reclaim dead local owners, never evict a live hour-long index builder."""
    path = Path(lock)
    deadline = time.monotonic() + timeout
    record = json.dumps(
        {
            "pid": os.getpid(),
            "created": psutil.Process().create_time(),
            "host": socket.gethostname(),
        }
    )
    while True:
        acquired = False
        with _reclaim_guard(path, deadline):
            if path.exists():
                if _abandoned(path, timeout):
                    path.unlink(missing_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                        stream.write(record)
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
                acquired = True
        if acquired:
            break
        if time.monotonic() >= deadline:
            raise WGSExtractError(
                f"Timed out waiting for index builder lock: {path}"
            ) from None
        logging.info(f"Waiting for another run to finish building {lock[:-5]}...")
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        with _reclaim_guard(path, time.monotonic() + timeout):
            if path.exists() and path.read_text() == record:
                path.unlink(missing_ok=True)
