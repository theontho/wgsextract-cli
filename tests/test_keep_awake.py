import subprocess
from unittest.mock import Mock

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam
from wgsextract_cli.core import keep_awake as awake


def test_disabled_does_not_probe_or_launch(monkeypatch):
    which = Mock(side_effect=AssertionError("must not probe"))
    monkeypatch.setattr(awake.shutil, "which", which)
    with awake.keep_awake(False):
        pass
    which.assert_not_called()


@pytest.mark.parametrize("failure", [None, OSError("denied")])
def test_linux_lifecycle_does_not_mask_command_failure(monkeypatch, failure):
    monkeypatch.setattr(awake.sys, "platform", "linux")
    monkeypatch.setattr(awake.shutil, "which", lambda name: "/usr/bin/" + name)
    process = Mock()
    process.poll.return_value = None
    process.stdin.close.side_effect = BrokenPipeError("helper already closed its pipe")
    launch = Mock(return_value=process, side_effect=failure)
    monkeypatch.setattr(awake.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="command failed"), awake.keep_awake(True):
        raise RuntimeError("command failed")
    if not failure:
        assert launch.call_args.args[0][:2] == [
            "/usr/bin/systemd-inhibit",
            "--what=idle:sleep",
        ]
        process.stdin.close.assert_called_once()
        process.terminate.assert_called_once()
        process.wait.assert_called_once()


def test_macos_helper_watches_current_process_and_is_reaped(monkeypatch):
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    monkeypatch.setattr(awake.shutil, "which", lambda name: "/usr/bin/" + name)
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 5), 0]
    launch = Mock(return_value=process)
    monkeypatch.setattr(awake.subprocess, "Popen", launch)
    with awake.keep_awake(True):
        pass
    assert launch.call_args.args[0] == [
        "/usr/bin/caffeinate",
        "-i",
        "-w",
        str(awake.os.getpid()),
    ]
    process.kill.assert_called_once()


@pytest.mark.parametrize("previous", [0, 0x80000002])
def test_windows_restores_previous_state_even_on_failure(monkeypatch, previous):
    monkeypatch.setattr(awake.sys, "platform", "win32")
    state = Mock(return_value=previous)
    monkeypatch.setattr(awake, "_windows_execution_state", lambda: state)
    with pytest.raises(ValueError), awake.keep_awake(True):
        raise ValueError("caller error")
    assert [call.args[0] for call in state.call_args_list] == (
        [0x80000001, previous] if previous else [0x80000001]
    )


def test_config_and_run_specific_disable_override(monkeypatch):
    from wgsextract_cli.cli.parser import build_parser, parse_args
    from wgsextract_cli.core.config import settings

    monkeypatch.setitem(settings, "keep_awake", True)
    args, _ = parse_args(*build_parser(), ["extract", "ydna-bam"])
    assert args.keep_awake is True
    args, _ = parse_args(*build_parser(), ["--no-keep-awake", "extract", "ydna-bam"])
    assert args.keep_awake is False
    args, _ = parse_args(*build_parser(), ["extract", "ydna-bam", "--no-keep-awake"])
    assert args.keep_awake is False


@needs_htslib
def test_tiny_extract_succeeds_with_unavailable_inhibitor(tmp_path, monkeypatch):
    from wgsextract_cli.main import main

    bam = sam_to_bam(
        ["@SQ\tSN:chrY\tLN:10", "read\t0\tchrY\t1\t60\t1M\t*\t0\t0\tA\tI"],
        tmp_path / "tiny.bam",
    )
    original_which = awake.shutil.which
    monkeypatch.setattr(
        awake.shutil,
        "which",
        lambda name: (
            None if name in ("caffeinate", "systemd-inhibit") else original_which(name)
        ),
    )
    # Only the inhibitor launch is unavailable; actual samtools still executes.
    main(
        [
            "extract",
            "ydna-bam",
            "--input",
            bam,
            "--outdir",
            str(tmp_path / "o"),
            "--keep-awake",
            "--threads",
            "2",
        ]
    )
    assert (tmp_path / "o" / "tiny_Y.bam").is_file()
