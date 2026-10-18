import os

from wgsextract_cli.cli import bootstrap


def test_parent_process_monitor_rejects_non_positive_pids(monkeypatch):
    def fail_if_called(parent_pid):
        raise AssertionError("Non-positive PIDs must not be probed")

    monkeypatch.setattr(bootstrap.psutil, "pid_exists", fail_if_called)

    assert bootstrap._parent_process_is_alive(0) is False
    assert bootstrap._parent_process_is_alive(-1) is False


def test_parent_process_monitor_detects_live_and_dead_pids(monkeypatch):
    monkeypatch.setattr(
        bootstrap.psutil, "pid_exists", lambda parent_pid: parent_pid == os.getpid()
    )

    assert bootstrap._parent_process_is_alive(os.getpid()) is True
    assert bootstrap._parent_process_is_alive(os.getpid() + 1) is False
