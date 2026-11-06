"""The terminal progress line: shown only on a terminal, and never mixed into
other output."""

import argparse
import contextlib
import io
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from wgsextract_cli.cli import dispatch
from wgsextract_cli.core import live_status, utils
from wgsextract_cli.core.progress import ContigProgress, Heartbeat
from wgsextract_cli.core.utils import popen, run_command, wait_with_cancel


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
    sys.stderr, root.handlers = tty, [handler]
    # setLevel, not assignment: it clears the isEnabledFor cache, which may
    # still say INFO is off from an earlier test.
    root.setLevel(logging.INFO)
    try:
        yield tty
    finally:
        sys.stderr, root.handlers = saved[0], saved[1]
        root.setLevel(saved[2])


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


@contextlib.contextmanager
def _pty_terminal() -> Iterator[tuple[io.TextIOWrapper, int, bytearray]]:
    """fds 1 and 2, sys.stdout and sys.stderr on a pseudo-terminal; yields the
    terminal stream, its fd, and what has reached the screen so far."""
    import pty

    main, side = pty.openpty()
    received = bytearray()
    reader = threading.Thread(
        target=lambda: [received.extend(c) for c in iter(lambda: _read(main), b"")]
    )
    reader.start()
    terminal = open(side, "w", buffering=1, closefd=False)  # noqa: SIM115
    saved = [os.dup(fd) for fd in (1, 2)]
    saved_streams = sys.stdout, sys.stderr
    try:
        for fd in (1, 2):
            os.dup2(side, fd)
        sys.stdout = sys.stderr = terminal
        yield terminal, side, received
    finally:
        sys.stdout, sys.stderr = saved_streams
        for fd, copy in zip((1, 2), saved, strict=True):
            os.dup2(copy, fd)
            os.close(copy)
        terminal.close()
        os.close(side)
        reader.join(timeout=5)
        os.close(main)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_tools_writing_to_the_terminal_do_not_overwrite_the_line():
    """A tool's own stdout and stderr, inherited or given the terminal, go
    through the line-clearing streams too."""
    write = "import sys; sys.stdout.write('out\\n'); sys.stderr.write('err\\n')"
    with _pty_terminal() as (terminal, side, received):
        with live_status.LiveStatus("vcf snp", terminal, delay=0, interval=0.01):
            _wait_for(lambda: b"vcf snp" in received)
            run_command([sys.executable, "-c", write], check=False)
            popen([sys.executable, "-c", write], stdout=side, stderr=side).wait()
        # Everything is on the terminal; wait for the pty to deliver it.
        _wait_for(lambda: received.count(b"out") == received.count(b"err") == 2)
    assert sorted(_screen(received.decode())) == ["err", "err", "out", "out"]


def _slow_relays(monkeypatch, seconds: float) -> None:
    """Relays that start late, as on a loaded machine."""
    relay = utils._tee_stderr

    def slow_relay(*args):
        time.sleep(seconds)
        relay(*args)

    monkeypatch.setattr(utils, "_tee_stderr", slow_relay)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
@pytest.mark.parametrize("finish", [lambda p: p.wait(), wait_with_cancel])
def test_a_started_tool_has_written_everything_once_it_has_finished(
    finish, monkeypatch
):
    """Once wait() or poll() says a popen'd tool is done, its relayed output
    is on the terminal, ahead of whatever is written next."""
    _slow_relays(monkeypatch, 0.3)
    with _pty_terminal() as (terminal, _side, received):
        with live_status.LiveStatus("vcf snp", terminal, delay=0, interval=0.01):
            finish(popen([sys.executable, "-c", "print('done')"]))
            sys.stdout.write("next\n")
        _wait_for(lambda: b"next" in received and b"done" in received)
    assert _screen(received.decode()) == ["done", "next"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_a_timed_wait_counts_relaying_against_its_timeout(monkeypatch):
    """The tool exits within the timeout, its relay only after it: the
    timeout covers both, not each in turn."""
    _slow_relays(monkeypatch, 1.4)
    tool = [sys.executable, "-c", "import time; time.sleep(0.6); print('done')"]
    with _pty_terminal() as (terminal, _side, _received):
        with live_status.LiveStatus("vcf snp", terminal, delay=0, interval=0.01):
            process = popen(tool)
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
            assert process.poll() is None
            assert process.wait(timeout=5) == 0


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, 4096)
    except OSError:  # the other side closed
        return b""


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


def test_finished_contigs_reach_the_line_through_progress_events():
    with _terminal() as tty:
        line = live_status.LiveStatus("vcf snp", tty, delay=0, interval=0.01)
        with line, dispatch._rendering_progress(line):
            ContigProgress(["chr1", "chr2"], {"chr1": 3, "chr2": 1}).done("chr1")
            _wait_for(lambda: "chr1 (1/2 contigs)" in tty.getvalue())
    assert _screen(tty.getvalue()) == []


def test_heartbeat_counts_reach_the_line_through_progress_events():
    with _terminal() as tty:
        line = live_status.LiveStatus("align", tty, delay=0, interval=0.01)
        with line, dispatch._rendering_progress(line):
            with Heartbeat("Alignment", 4, interval=60, emit=False) as heartbeat:
                heartbeat.advance(2)
                heartbeat.report()
                _wait_for(lambda: re.search(r"align, \d+s, 2/4 reads", tty.getvalue()))
    assert _screen(tty.getvalue()) == []
