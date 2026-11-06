"""Which recorded paths a run may follow.

A command that follows paths other runs recorded (``report`` reads the outputs
their manifests list) asks :func:`may_follow` first. A server job limits that
to the folders the server allows, so a manifest can't lead it elsewhere; on
the command line every path may be followed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ALLOWS: ContextVar[Callable[[str], bool] | None] = ContextVar(
    "read_scope", default=None
)


@contextmanager
def limited_to(allows: Callable[[str], bool]) -> Iterator[None]:
    """Follow only the recorded paths ``allows`` accepts, until the end."""
    token = _ALLOWS.set(allows)
    try:
        yield
    finally:
        _ALLOWS.reset(token)


def may_follow(path: str | os.PathLike[str]) -> bool:
    allows = _ALLOWS.get()
    return allows is None or allows(os.fspath(path))
