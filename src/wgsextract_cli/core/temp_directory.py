"""Optional owned run workspace on a user-selected temporary volume."""

import contextlib
import os
import tempfile
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path

from wgsextract_cli.core.paths import PathInput, absolute_path, expand_path
from wgsextract_cli.core.utils import WGSExtractError

_workspace: ContextVar[str | None] = ContextVar("temporary_workspace", default=None)


def configured_temp_directory() -> str | None:
    active = _workspace.get()
    if active is not None:
        return active
    from wgsextract_cli.core.config import settings

    configured = settings.get("temp_directory")
    return str(absolute_path(expand_path(str(configured)))) if configured else None


@contextlib.contextmanager
def run_temporary_directory(override: PathInput | None = None) -> Iterator[None]:
    parent = (
        str(absolute_path(expand_path(override)))
        if override
        else configured_temp_directory()
    )
    if parent is None:
        yield
        return
    if not Path(parent).is_dir() or not os.access(parent, os.W_OK):
        raise WGSExtractError(
            f"Temporary directory must exist and be writable: {parent}. Create it first, or choose another --tmp-dir."
        )
    try:
        workspace = tempfile.TemporaryDirectory(dir=parent, prefix=".wgse_run_")
    except OSError as error:
        raise WGSExtractError(
            f"Cannot use temporary directory {parent}: {error}"
        ) from error
    with workspace as directory:
        token = _workspace.set(directory)
        try:
            yield
        finally:
            _workspace.reset(token)
