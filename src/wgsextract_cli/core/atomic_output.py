"""Publish one completed output without replacing a previously valid file."""

import contextlib
import os
import stat
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path

from wgsextract_cli.core.paths import PathInput

_enabled: ContextVar[bool | None] = ContextVar("atomic_outputs", default=None)


def atomic_outputs_enabled() -> bool:
    from wgsextract_cli.core.config import settings

    override = _enabled.get()
    return bool(settings.get("atomic_outputs", False)) if override is None else override


@contextlib.contextmanager
def atomic_output_mode(enabled: bool) -> Iterator[None]:
    token = _enabled.set(enabled)
    try:
        yield
    finally:
        _enabled.reset(token)


@contextlib.contextmanager
def safe_output(path: PathInput) -> Iterator[str]:
    if atomic_outputs_enabled():
        with atomic_output(path) as staged:
            yield staged
    else:
        yield str(path)


def _indexes(path: Path) -> list[Path]:
    indexes = [
        Path(str(path) + ext) for ext in (".bai", ".crai", ".csi", ".tbi", ".idx")
    ]
    alternate = {".bam": ".bai", ".cram": ".crai"}.get(path.suffix.lower())
    if alternate:
        indexes.append(path.with_suffix(alternate))
    return indexes


@contextlib.contextmanager
def atomic_output(path: PathInput, *, retire_indexes: bool = True) -> Iterator[str]:
    final = Path(path)
    suffix = "".join(final.suffixes)
    base = final.name[: -len(suffix)] if suffix else final.name
    staged = final.with_name(f".{base}.{uuid.uuid4().hex}.partial{suffix}")
    retired: list[tuple[Path, Path]] = []
    try:
        yield str(staged)
        if not staged.is_file():
            from wgsextract_cli.core.utils import WGSExtractError

            raise WGSExtractError(f"Output writer did not produce {final}")
        try:
            for number, index in enumerate(_indexes(final) if retire_indexes else []):
                if index.exists() or index.is_symlink():
                    backup = Path(f"{staged}.previous-{number}{index.suffix}")
                    os.replace(index, backup)
                    retired.append((index, backup))
            os.replace(staged, final)
        except BaseException:
            for index, backup in retired:
                os.replace(backup, index)
            raise
        else:
            for _, backup in retired:
                backup.unlink(missing_ok=True)
    finally:
        staged.unlink(missing_ok=True)
        for index in _indexes(staged):
            index.unlink(missing_ok=True)


def atomic_write(path: PathInput, data: bytes) -> None:
    final = Path(path)
    mode = stat.S_IMODE(final.stat().st_mode) if final.exists() else 0o600
    with atomic_output(final, retire_indexes=False) as staged:
        fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
