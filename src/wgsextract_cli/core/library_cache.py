"""Derived microarray caches in the reference library, named to fit MAX_PATH.

Native Windows tools such as htslib open files through the 260-character Win32
``MAX_PATH`` limit whatever ``LongPathsEnabled`` says, so every cache path below
the library stays short: content keys are truncated digests and directories
nest at most one key deep.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from wgsextract_cli.core.results import warn

if TYPE_CHECKING:
    from hashlib import _Hash

# 80 bits. Keys are accidental, not adversarial: even 10**6 cached entries
# collide with probability ~n**2 / 2**81 < 1e-12, and every entry is checksummed.
KEY_LENGTH = 20
# The longest path below the library, from its separator onwards:
# /microarray/chm13/<key>/target-<key>.tab.gz.lock.guard (84 characters).
LONGEST_SUFFIX = 84
WINDOWS_MAX_PATH = 260
# Before #419 caches were named by whole SHA-256 digests; nothing else is.
_LEGACY_NAME = re.compile(r"[0-9a-f]{64}")


def short_key(digest: _Hash) -> str:
    return digest.hexdigest()[:KEY_LENGTH]


def microarray_cache_root(library: str | None) -> Path | None:
    """``<library>/microarray`` for derived caches, or None to run uncached.

    On Windows a library too deep for its longest cache path is reported and
    skipped rather than failing later with a misleading "No such file" from
    htslib. Caches left in the old full-digest layout are removed; they are
    never read again.
    """
    if not library:
        return None
    root = Path(library, "microarray")
    depth = len(str(Path(library).absolute()))
    if sys.platform == "win32" and depth + LONGEST_SUFFIX >= WINDOWS_MAX_PATH:
        warn(
            "cache_path_too_long",
            f"Reference library path is too long ({depth} characters) for "
            f"Windows tools to open its microarray caches, so this run is "
            f"uncached. Move reference_library to a folder path of at most "
            f"{WINDOWS_MAX_PATH - LONGEST_SUFFIX - 1} characters "
            f"(for example C:\\WGSExtract\\reference).",
            path=str(library),
            length=depth,
            limit=WINDOWS_MAX_PATH - LONGEST_SUFFIX - 1,
        )
        return None
    for directory in (root / "chm13", root / "hg19-coordinates"):
        _prune_legacy(directory)
    return root


def _prune_legacy(directory: Path) -> None:
    try:
        entries = [e for e in directory.iterdir() if _LEGACY_NAME.search(e.name)]
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            # Read-only libraries and files held open by another run keep them.
            pass
