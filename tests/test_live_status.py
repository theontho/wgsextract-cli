"""The terminal progress line: shown only on a terminal, and never mixed into
other output."""

import argparse
import contextlib
import io
import logging
import sys
import time
from collections.abc import Iterator

from wgsextract_cli.cli import dispatch
from wgsextract_cli.core import live_status
from wgsextract_cli.core.utils import run_command


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _screen(raw: str) -> list[str]:
    """What a terminal shows after raw: '\\r' returns to the line start and
    later characters overwrite earlier ones."""
    lines, line, column = [], [], 0
    for char in raw:
        if char == "\n":
            lines.append("".join(line).rstrip())
            line, column = [], 0
        elif char == "\r":
            column = 0
        else:
            line[column : column + 1] = [char]
            column += 1
    lines.append("".join(line).rstrip())
    return [text for text in lines if text]


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@contextlib.contextmanager
def _terminal() -> Iterator[_Tty]:
    """stderr and the log handler on a fake terminal. (Set inside the test:
    pytest's output capture replaces sys.stderr after fixtures run.)"""
    tty = _Tty()
    handler = logging.StreamHandler(tty)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root = logging.getLogger()
    saved = sys.stderr, root.handlers[:], root.level
    sys.stderr, root.handlers, root.level = tty, [handler], logging.INFO
    try:
        yield tty
    finally:
        sys.stderr, root.handlers, root.level = saved


def test_the_line_shows_the_step_and_clears_before_other_output():
    with _terminal() as tty:
        with live_status.LiveStatus("microarray", tty, delay=0, interval=0.01):
            with live_status.step("samtools mpileup"):
                _wait_for(lambda: "samtools mpileup" in tty.getvalue())
                logging.info("Writing the 23andMe file")
                run_command(
                    [sys.executable, "-c", "import sys; sys.stderr.write('note\\n')"]
                )
                _wait_for(lambda: "mpileup" in tty.getvalue().rsplit("\n", 1)[-1])
        assert sys.stderr is tty
    assert "\x1b" not in tty.getvalue()
    assert _screen(tty.getvalue()) == ["INFO: Writing the 23andMe file", "note"]


def test_the_line_names_the_running_tool():
    with _terminal() as tty:
        with live_status.LiveStatus("vcf snp", tty, delay=0, interval=0.01):
            run_command([sys.executable, "-c", "import time; time.sleep(0.3)"])
    assert "vcf snp: python" in tty.getvalue()


def _namespace(**values) -> argparse.Namespace:
    return argparse.Namespace(**{"json": False, "quiet": False, **values})


def test_only_long_commands_at_a_terminal_without_json_or_quiet_get_it(
    monkeypatch,
):
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(sys, "stderr", _Tty())

    def shown(args: argparse.Namespace, command: str) -> bool:
        return isinstance(
            dispatch._progress_line(args, command), live_status.LiveStatus
        )

    assert shown(_namespace(), "microarray")
    assert shown(_namespace(), "align")
    assert not shown(_namespace(json=True), "microarray")
    assert not shown(_namespace(quiet=True), "microarray")
    assert not shown(_namespace(), "info")  # seconds
    assert not shown(_namespace(), "ref download")  # curl draws its own bar
    monkeypatch.setenv("TERM", "dumb")
    assert not shown(_namespace(), "microarray")
    monkeypatch.delenv("TERM")
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert not shown(_namespace(), "microarray")


def test_steps_do_nothing_without_a_display():
    with live_status.step("samtools view"):
        live_status.detail("10 reads")
