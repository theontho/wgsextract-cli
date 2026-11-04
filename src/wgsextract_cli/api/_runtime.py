"""Running one operation: the result it records into, progress and cancellation."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from wgsextract_cli.core.results import (
    CommandResult,
    collecting,
    fail_from_exception,
    stdout_reserved,
)

from ._params import Params, with_config_defaults

P = TypeVar("P", bound=Params)

# Exit code of a cancelled operation, as for a CLI run stopped with Ctrl-C.
EXIT_CANCELLED = 130


@dataclass(frozen=True)
class ProgressEvent:
    """One progress report: a stage name, a message and, when known, how far
    through the operation is (0.0 to 1.0).

    Stages so far: ``start`` and ``finish`` (the command name, 0.0 and 1.0)
    and ``tool`` (an external tool is starting). More stages may be added."""

    stage: str
    message: str = ""
    fraction: float | None = None


class ProgressCallback(Protocol):
    """Receives progress events, from the calling thread or a worker thread."""

    def __call__(self, event: ProgressEvent, /) -> None: ...


class CancelToken:
    """Cooperative cancellation of a running operation.

    ``cancel()`` may be called from any thread. It stops the operation's
    external tools at once and makes the operation fail as cancelled at its
    next checkpoint. A token belongs to one operation.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logging.debug("Cancel callback failed.", exc_info=True)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        """Alias of ``cancelled``, so a token works where a ``threading.Event`` does."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("Cancelled.")

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` on cancellation (at once if already cancelled);
        returns a function that unregisters it."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return lambda: self._remove(callback)
        callback()
        return lambda: None

    def _remove(self, callback: Callable[[], None]) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._callbacks.remove(callback)


class Cancelled(Exception):
    """Raised inside an operation when its ``CancelToken`` was cancelled."""


class CommandFailed(Exception):
    """An operation failed; ``result`` holds the error, warnings and partial outputs."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        error = result.error
        super().__init__(error.message if error else f"exit code {result.exit_code}")

    @property
    def exit_code(self) -> int:
        return self.result.exit_code


@dataclass
class _Context:
    progress: ProgressCallback | None = None
    cancel: CancelToken | None = None


# Like the active result, the progress sink is process-wide so worker threads
# reach it too; operations run one at a time (see ``_LOCK``).
_context = _Context()
_LOCK = threading.RLock()


def report(stage: str, message: str = "", fraction: float | None = None) -> None:
    """Send a progress event to the running operation's callback, if any."""
    callback = _context.progress
    if callback is None:
        return
    try:
        callback(ProgressEvent(stage, message, fraction))
    except Exception:
        logging.debug("Progress callback failed.", exc_info=True)


def checkpoint() -> None:
    """Raise ``Cancelled`` if the running operation was cancelled."""
    if _context.cancel is not None:
        _context.cancel.raise_if_cancelled()


def _before_tool(command: Sequence[str]) -> None:
    checkpoint()
    if command:
        report("tool", f"Running {Path(command[0]).name}.")


def cancel_event() -> CancelToken | None:
    """The running operation's token, for ``run_pipeline(cancel_event=...)``."""
    return _context.cancel


@contextlib.contextmanager
def _context_of(
    progress: ProgressCallback | None, cancel: CancelToken | None
) -> Iterator[None]:
    previous = (_context.progress, _context.cancel)
    _context.progress, _context.cancel = progress, cancel
    try:
        yield
    finally:
        _context.progress, _context.cancel = previous


def run_inline(params: P, impl: Callable[[P], None]) -> None:
    """Run ``impl`` inside the caller's active result (the CLI's dispatch)."""
    impl(with_config_defaults(params))


def execute(
    params: P,
    impl: Callable[[P], None],
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
    write_manifest: bool = True,
) -> CommandResult:
    """Run one operation as a library call and return its result.

    Human-readable text is not printed, and while the operation runs anything
    written to stdout (by external tools or other threads) goes to stderr, so
    stdout stays free for a caller's own protocol. With ``check`` a failed or cancelled
    operation raises ``CommandFailed`` carrying the result; without it the
    result is returned with ``status`` and ``error`` set. The manifest is
    written next to the outputs as the CLI does, unless ``write_manifest`` is
    false. ``KeyboardInterrupt`` propagates.
    """
    from wgsextract_cli.core.atomic_output import atomic_output_mode
    from wgsextract_cli.core.config import settings
    from wgsextract_cli.core.process_registry import proc_registry
    from wgsextract_cli.core.run_log import tool_hook
    from wgsextract_cli.core.temp_directory import run_temporary_directory

    with _LOCK:
        params = with_config_defaults(params)
        result = CommandResult(command=params.COMMAND)
        result.provenance.params = params.to_dict()
        unregister = cancel.on_cancel(proc_registry.cleanup) if cancel else None
        error: BaseException | None = None
        with (
            stdout_reserved(),
            collecting(result, as_json=True),
            _context_of(progress, cancel),
            tool_hook(_before_tool),
        ):
            try:
                checkpoint()
                report("start", params.COMMAND, 0.0)
                _prepare(params, result)
                with (
                    atomic_output_mode(bool(settings.get("atomic_outputs", False))),
                    run_temporary_directory(None),
                ):
                    impl(params)
                checkpoint()
                report("finish", params.COMMAND, 1.0)
            except (Exception, SystemExit) as e:
                error = e
                if cancel is not None and cancel.cancelled:
                    logging.error("Cancelled.")
                    result.fail("cancelled", "Cancelled.", EXIT_CANCELLED)
                else:
                    fail_from_exception(result, e)
            finally:
                if unregister is not None:
                    unregister()
        result.finalize(probe_tools=True)
        if write_manifest:
            try:
                result.write_manifest()
            except OSError as e:
                logging.warning(f"Could not write the result manifest: {e}")
    if check and result.exit_code:
        raise CommandFailed(result) from error
    return result


def _prepare(params: Params, result: CommandResult) -> None:
    """What dispatch does before a command: record and check the input, make
    the output folder."""
    input_path = getattr(params, "input", None)
    if isinstance(input_path, Path):
        if input_path.expanduser().exists():
            result.add_input(input_path, "input")
        if input_path.suffix.lower() in {".bam", ".cram"}:
            from wgsextract_cli.core.alignment_input import validate_alignment_input

            validate_alignment_input(input_path)
    outdir = getattr(params, "outdir", None)
    if isinstance(outdir, Path):
        outdir.expanduser().mkdir(parents=True, exist_ok=True)
