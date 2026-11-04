"""The record of the most recent CLI run, kept for 'wgsextract report-issue'.

Each dispatched command (except help, config and report-issue itself) leaves
its result manifest and the tail of its log in the state directory (the
``state_directory`` setting, else ``state/`` in the data folder):
``last-run.json``, and also ``last-failed-run.json`` when it failed. Both stay on this computer, readable only by the user; a report
redacts what it takes from them.
"""

from __future__ import annotations

import collections
import json
import logging
from pathlib import Path
from typing import Any

from wgsextract_cli.core.results import CommandResult

SCHEMA_ID = "wgsextract.last-run/1"
LOG_TAIL_LINES = 200

# Commands that never replace the record: reporting on the last run must not
# report on itself, and help/config don't run anything worth reporting.
UNRECORDED_COMMANDS = frozenset({"help", "config", "report-issue"})


def state_directory() -> Path:
    """The ``state_directory`` setting, else ``state`` in the data folder (beside
    reference/ and genomes/, so uninstalling removes it)."""
    from wgsextract_cli.core.config import ensure_settings_loaded, settings

    ensure_settings_loaded()
    configured = settings.get("state_directory")
    return Path(configured).expanduser() if configured else _default_state_directory()


def _default_state_directory() -> Path:
    from wgsextract_cli.core.runtime import repo_root

    return repo_root() / "state"


def last_run_path(*, failed: bool = False) -> Path:
    name = "last-failed-run.json" if failed else "last-run.json"
    return state_directory() / name


class LogTail(logging.Handler):
    """Keeps the last ``LOG_TAIL_LINES`` formatted log records in memory."""

    def __init__(self, capacity: int = LOG_TAIL_LINES) -> None:
        super().__init__(logging.DEBUG)
        self.lines: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.extend(self.format(record).splitlines() or [""])
        except Exception:
            self.handleError(record)


def record_last_run(
    result: CommandResult, *, log_tail: list[str], log_file: Path | None
) -> None:
    """Save ``result`` as the last run (and last failed run, if it failed).

    Never fails the command: an unwritable data folder is logged at debug."""
    if result.command.split(" ", 1)[0] in UNRECORDED_COMMANDS:
        return
    from wgsextract_cli.core.atomic_output import atomic_write

    document = {
        "schema": SCHEMA_ID,
        "result": result.to_dict(),
        "log_file": str(log_file) if log_file else None,
        "log_tail": log_tail[-LOG_TAIL_LINES:],
    }
    data = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    targets = [last_run_path()]
    if result.exit_code:
        targets.append(last_run_path(failed=True))
    try:
        state_directory().mkdir(parents=True, exist_ok=True)
        for path in targets:
            atomic_write(path, data)
    except OSError as e:
        logging.debug(f"Could not record the last run: {e}")


def load_last_run(*, failed: bool = False) -> dict[str, Any] | None:
    """The saved record, or None when there is none or it is unreadable."""
    path = last_run_path(failed=failed)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA_ID:
        return None
    return document
