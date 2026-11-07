"""``deps check``: which bioinformatics tools this installation finds, where,
and in what version.

``check`` without ``tool`` lists every mandatory and optional tool. The
result's ``details`` hold ``mandatory`` and ``optional``: one
``{name, path, runtime, version, alt_env}`` per tool (``path`` is ``None`` for
a missing one). It fails with exit code 3 (error kind
``missing_dependency``) if a mandatory tool is missing. With ``tool``, it
checks that one tool: its path is ``details["path"]`` and the result's text,
and a missing tool fails the same way, naming the command that installs it
when there is one."""

from __future__ import annotations

from dataclasses import dataclass

from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute


@dataclass(frozen=True, kw_only=True)
class DepsCheckParams(Params):
    """Check the installed tools, or one ``tool``."""

    COMMAND = "deps check"

    tool: str | None = param(
        None,
        help="Check for a specific tool; exit code 3 if it is missing.",
        doc="The one tool to check (e.g. samtools); fails if it is missing.",
        may_name_file=True,
    )


def check(
    params: DepsCheckParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Check the installed tools (see ``DepsCheckParams``)."""
    from wgsextract_cli.commands._deps_status import run

    return execute(params, run, progress=progress, cancel=cancel, check=check)


__all__ = ["DepsCheckParams", "check"]
