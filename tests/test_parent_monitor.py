import os
import sys
from types import SimpleNamespace

from wgsextract_cli import main


def test_parent_process_monitor_uses_psutil_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(pid_exists=lambda parent_pid: parent_pid == os.getpid()),
    )

    def fail_if_called(parent_pid, signal_number):
        raise AssertionError("Windows parent liveness checks must not call os.kill")

    monkeypatch.setattr(main.os, "kill", fail_if_called)

    assert main._parent_process_is_alive(os.getpid()) is True


def test_parent_process_monitor_posix_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []

    def fake_kill(parent_pid, signal_number):
        calls.append((parent_pid, signal_number))

    monkeypatch.setattr(main.os, "kill", fake_kill)

    parent_pid = os.getpid()
    assert main._parent_process_is_alive(parent_pid) is True
    assert calls == [(parent_pid, 0)]
