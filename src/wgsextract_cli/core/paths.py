"""Filesystem paths shared by the CLI and its string-based tool boundaries."""

import logging
from os import PathLike, fspath
from pathlib import Path
from typing import TypeAlias

PathInput: TypeAlias = str | PathLike[str]


def absolute_path(path: PathInput) -> Path:
    """Normalize dot segments without resolving symlinks, unlike Path.resolve()."""
    absolute = Path(path).absolute()
    parts: list[str] = []
    for part in absolute.parts[1:]:
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return Path(absolute.anchor, *parts)


def expand_path(path: PathInput) -> str:
    """Expand a leading tilde without changing command strings or empty settings."""
    value = fspath(path)
    return str(Path(value).expanduser()) if value.startswith("~") else value


def command_is_path(command: str) -> bool:
    """A wrapper command may exceed filesystem name limits on Python < 3.14."""
    try:
        return Path(command).exists()
    except OSError as exc:
        logging.debug("Command is not a filesystem path (errno %s).", exc.errno)
        return False


def append_suffix(path: PathInput, suffix: str) -> Path:
    """Append a sidecar suffix without replacing the file's existing extension."""
    file = Path(path)
    return file.with_name(file.name + suffix)
