"""Publish complete BWA index sets, never an interrupted staged build."""

import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.utils import WGSExtractError, run_command

INDEX_SUFFIXES = (".amb", ".ann", ".bwt", ".pac", ".sa")


def has_index(ref: str, suffixes: Sequence[str], tool: str) -> bool:
    if Path(f"{ref}.{tool}.pending").exists():
        return False
    try:
        return all(
            Path(ref + suffix).is_file() and Path(ref + suffix).stat().st_size > 0
            for suffix in suffixes
        )
    except OSError:
        return False


@contextlib.contextmanager
def staged_index(ref: str, suffixes: Sequence[str], tool: str) -> Iterator[str]:
    pending = Path(f"{ref}.{tool}.pending")
    complete = Path(f"{ref}.{tool}.complete")
    pending.write_text("building\n", encoding="utf-8", newline="\n")
    complete.unlink(missing_ok=True)
    # A killed subprocess leaves only this owned staging directory. The next
    # lock holder discards it before rebuilding, not the final index prefix.
    parent = Path(ref).parent
    prefix = f".{Path(ref).name}.{tool}-index-"
    for abandoned in parent.glob(prefix + "*"):
        if abandoned.is_dir() and not abandoned.is_symlink():
            shutil.rmtree(abandoned)
    with tempfile.TemporaryDirectory(dir=parent, prefix=prefix) as stage:
        staged = str(Path(stage) / "index")
        yield staged
        if not has_index(staged, suffixes, tool):
            raise WGSExtractError(f"{tool} produced an incomplete index for {ref}")
        for suffix in suffixes:
            os.replace(staged + suffix, ref + suffix)
        complete.write_text("complete\n", encoding="utf-8", newline="\n")
        pending.unlink()


def ensure_index(ref: str, runner: Callable[..., object] = run_command) -> None:
    if has_index(ref, INDEX_SUFFIXES, "bwa"):
        return
    with build_lock(ref + ".bwa-index.lock"):
        if has_index(ref, INDEX_SUFFIXES, "bwa"):
            return
        logging.info(f"BWA index missing or incomplete for {ref}. Generating now...")
        with staged_index(ref, INDEX_SUFFIXES, "bwa") as staged:
            runner(["bwa", "index", "-p", staged, ref])
