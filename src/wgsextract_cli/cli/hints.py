"""'Next:' hints after a command succeeds, from commands/_next_steps.py.

They go to stderr, and only on a terminal: never with --json or --quiet, or
when stderr is piped, so scripts and agents see the same output as before.
"""

import argparse
import os
import re
import shlex
import sys
from collections.abc import Iterable
from typing import IO

from wgsextract_cli.commands._next_steps import NEXT_STEPS
from wgsextract_cli.core.results import CommandResult, FileRecord

from .help import _heading, use_color

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SAFE = re.compile(r"[\w@%+=:,./~-]+")


def next_steps(
    command: str, args: argparse.Namespace, outputs: Iterable[FileRecord]
) -> list[str]:
    """The filled-in hints for a finished command; a hint whose placeholder
    this run can't fill is left out. An --input given with --genome, and an
    explicit --ref or --config, are kept."""
    explicit: set[str] = getattr(args, "_explicit_dests", set())
    values: dict[str, str] = {}
    genome = getattr(args, "genome", None)
    source = getattr(args, "input", None)
    words = []
    if genome:
        words.append(f"--genome {quote(str(genome))}")
    if source and (not genome or "input" in explicit):
        words.append(f"--input {quote(str(source))}")
    if words:
        values["source"] = " ".join(words)
    # Settings the user chose for this run must carry over to the next.
    context = "".join(
        f" {flag} {quote(str(getattr(args, dest)))}"
        for dest, flag in (("ref", "--ref"), ("config_path", "--config"))
        if dest in explicit and getattr(args, dest, None)
    )
    outputs = list(outputs)
    for name, kinds in (("bam", {"bam", "cram"}), ("vcf", {"vcf"})):
        found = next((o.path for o in outputs if o.kind in kinds), None)
        if found:
            values[name] = quote(_display_path(found))
    hints = []
    for template in NEXT_STEPS.get(command, ()):
        names = set(_PLACEHOLDER.findall(template))
        if names <= values.keys():
            hints.append(_PLACEHOLDER.sub(lambda m: values[m[1]], template) + context)
    return hints


def print_next_steps(
    args: argparse.Namespace, result: CommandResult, stream: IO[str] | None = None
) -> None:
    """Print 'Next:' hints for a successful run, if the user is at a terminal."""
    stream = sys.stderr if stream is None else stream
    if (
        result.exit_code
        or getattr(args, "json", False)
        or getattr(args, "quiet", False)
        or not _isatty(stream)
    ):
        return
    hints = next_steps(result.command, args, result.outputs)
    if not hints:
        return
    lines = [_heading("Next:", use_color(stream))]
    lines += [f"  {hint}" for hint in hints]
    stream.write("\n".join(lines) + "\n")


def quote(value: str) -> str:
    """value as one shell word: POSIX quoting, or double quotes on Windows."""
    if _SAFE.fullmatch(value):
        return value
    return f'"{value}"' if os.name == "nt" else shlex.quote(value)


def _display_path(path: str) -> str:
    """path relative to the working directory when it's below it."""
    try:
        relative = os.path.relpath(path)
    except ValueError:  # another drive on Windows
        return path
    return path if relative.startswith("..") else relative


def _isatty(stream: IO[str]) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
