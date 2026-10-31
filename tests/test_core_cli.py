"""Regression tests for CLI bootstrap, config, and core runtime helpers."""

import csv
import logging
import subprocess
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from wgsextract_cli.cli import bootstrap, parser
from wgsextract_cli.commands import _deps_status, examples
from wgsextract_cli.core import config, process_registry, resource_policy
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def temp_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: config_path)
    saved = dict(config.settings)
    yield config_path
    config.settings.clear()
    config.settings.update(saved)


@pytest.mark.parametrize("value", [None, "auto", "AUTO", " auto "])
def test_parse_thread_count_auto(value):
    assert resource_policy.parse_thread_count(value) is None


@pytest.mark.parametrize("value", ["0", "-2", "many", "1.5"])
def test_parse_thread_count_rejects_invalid(value):
    with pytest.raises(ValueError):
        resource_policy.parse_thread_count(value)


def test_resource_defaults_accept_auto():
    threads, memory = resource_policy.get_resource_defaults(
        "auto", "auto", lambda: Namespace(threads=6)
    )
    assert threads == "6"
    assert memory.endswith("G")


def test_config_threads_auto_becomes_none_default(temp_config):
    temp_config.write_text('threads = "auto"\n')
    config.reload_settings()
    cli_parser, base_parser = parser.build_parser()

    args, _ = parser.parse_args(cli_parser, base_parser, ["info"])
    assert args.threads is None

    args, explicit = parser.parse_args(
        cli_parser, base_parser, ["--threads", "auto", "info"]
    )
    assert args.threads is None
    assert "threads" in explicit

    args, _ = parser.parse_args(cli_parser, base_parser, ["info", "-t", "3"])
    assert args.threads == 3


def test_invalid_threads_flag_is_a_usage_error(temp_config):
    config.reload_settings()
    cli_parser, base_parser = parser.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(cli_parser, base_parser, ["info", "--threads", "0"])
    assert exc.value.code == 2


def test_invalid_config_threads_is_ignored_with_warning(temp_config, caplog):
    temp_config.write_text('threads = "max"\n')
    config.reload_settings()
    with caplog.at_level(logging.WARNING):
        cli_parser, base_parser = parser.build_parser()
    assert "Ignoring invalid cpu_threads" in caplog.text

    args, _ = parser.parse_args(cli_parser, base_parser, ["info"])
    assert args.threads is None
    args, _ = parser.parse_args(cli_parser, base_parser, ["info", "--threads", "4"])
    assert args.threads == 4


def test_save_config_clears_aliases_of_cleared_key(temp_config):
    temp_config.write_text('reflib = "/old/reference"\nthreads = 4\n')
    config.reload_settings()
    assert config.settings["reference_library"] == "/old/reference"

    config.save_config({"reference_library": None})

    assert "reference_library" not in config.settings
    assert "reflib" not in temp_config.read_text()
    assert config.settings["cpu_threads"] == 4


def test_save_config_refuses_to_overwrite_invalid_toml(temp_config):
    temp_config.write_text("this is = = not toml\n")
    with pytest.raises(WGSExtractError):
        config.save_config({"reference_library": "/x"})
    assert temp_config.read_text() == "this is = = not toml\n"


def test_full_help_unescapes_percent_signs(capsys):
    cli_parser, _ = parser.build_parser()
    parser.print_full_help(cli_parser)
    out = capsys.readouterr().out
    assert "%%" not in out
    assert "smaller than BAM" in out


def test_emoji_formatter_does_not_mutate_record():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "msg", None, None)
    formatted = bootstrap.EmojiFormatter("%(levelname)s %(message)s").format(record)
    assert formatted.endswith("msg")
    assert "WARNING" not in formatted
    assert record.levelname == "WARNING"


def test_registry_cleanup_reenters_while_lock_held():
    registry = process_registry.ProcessRegistry()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        **process_registry.process_group_kwargs(),
    )
    registry.register_process("sleep", proc)
    done = threading.Event()

    def cleanup_under_lock():
        # Simulates a signal handler interrupting register/unregister.
        with registry._lock:
            registry.cleanup()
        done.set()

    worker = threading.Thread(target=cleanup_under_lock, daemon=True)
    worker.start()
    assert done.wait(5), "cleanup deadlocked while the registry lock was held"
    assert proc.wait(5) is not None
    assert registry.processes == {}


def test_windows_cleanup_terminates_tree_before_launcher_exits(monkeypatch):
    registry = process_registry.ProcessRegistry()
    proc = Mock(pid=1234)
    proc.poll.side_effect = [None, 0]
    registry.register_process("gatk", proc)
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(process_registry.sys, "platform", "win32")
    monkeypatch.setattr(process_registry.subprocess, "run", run)
    monkeypatch.setattr(process_registry.time, "sleep", lambda _: None)

    registry.cleanup()

    run.assert_called_once_with(
        ["taskkill", "/F", "/T", "/PID", "1234"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    proc.send_signal.assert_not_called()
    proc.kill.assert_not_called()
    assert registry.processes == {}


def test_windows_cleanup_falls_back_when_taskkill_times_out(monkeypatch, caplog):
    registry = process_registry.ProcessRegistry()
    proc = Mock(pid=1234)
    proc.poll.return_value = None
    registry.register_process("gatk", proc)
    monkeypatch.setattr(process_registry.sys, "platform", "win32")
    monkeypatch.setattr(
        process_registry.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired("taskkill", 10)),
    )
    monkeypatch.setattr(process_registry.time, "sleep", lambda _: None)

    registry.cleanup()

    proc.kill.assert_called_once()
    assert "Process 1234 could not be terminated" in caplog.text
    assert registry.processes == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-tree regression")
def test_windows_cleanup_kills_break_ignoring_descendants(tmp_path):
    child_pid = tmp_path / "child.pid"
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
        f"Path({str(grandchild_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    child = (
        "import os, signal, subprocess, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)",
        ],
        **process_registry.process_group_kwargs(),
    )
    registry = process_registry.ProcessRegistry()
    registry.register_process("launcher", parent)
    descendants = []
    try:
        deadline = time.monotonic() + 15
        while not all(
            path.exists() and path.stat().st_size > 0
            for path in (child_pid, grandchild_pid)
        ):
            assert parent.poll() is None
            assert time.monotonic() < deadline, "descendants did not initialize"
            time.sleep(0.05)
        descendants = [int(path.read_text()) for path in (child_pid, grandchild_pid)]
        registry.cleanup()
        parent.wait(timeout=5)
        for pid in descendants:
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            assert not any(
                len(row) > 1 and row[1] == str(pid)
                for row in csv.reader(listing.stdout.splitlines())
            ), f"descendant {pid} survived cleanup"
        descendants.clear()
    finally:
        remaining = descendants + ([parent.pid] if parent.poll() is None else [])
        for pid in remaining:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
        parent.wait(timeout=5)


def test_deps_check_exits_nonzero_when_mandatory_missing(monkeypatch, capsys):
    monkeypatch.setattr(
        _deps_status,
        "check_all_dependencies",
        lambda: {
            "mandatory": [
                {
                    "name": "samtools",
                    "path": None,
                    "runtime": "missing",
                    "version": None,
                }
            ],
            "optional": [],
        },
    )
    with pytest.raises(WGSExtractError, match="samtools"):
        _deps_status.run(Namespace(tool=None, debug=False))


def _download_args(root: Path, force: bool) -> Namespace:
    return Namespace(
        example_ids=["phase3-chrmt-vcf"],
        all=False,
        tag=None,
        method="https",
        target_root=str(root),
        aspera_key=None,
        force=force,
        dry_run=False,
    )


def _fake_http_download(calls):
    def download(_source, destination):
        output = Path(destination)
        partial = examples._partial_path(output)
        calls.append(partial.read_bytes() if partial.exists() else None)
        with partial.open("ab") as f:
            f.write(b"rest")
        partial.replace(output)
        return True

    return download


def test_example_download_resumes_partial_and_skips_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(examples.shutil, "which", lambda _name: "/usr/bin/curl")
    calls: list[bytes | None] = []
    monkeypatch.setattr(examples, "download_file", _fake_http_download(calls))
    example = examples._select_examples(["phase3-chrmt-vcf"], False)[0]
    example_dir = tmp_path / examples.COLLECTION_DIR / example.example_id
    example_dir.mkdir(parents=True)
    planned = examples._planned_downloads(example, example_dir, "https")
    first, second = (destination for _s, destination, _r, _m in planned)
    first.write_bytes(b"complete")
    examples._partial_path(second).write_bytes(b"half-")

    examples.cmd_download(_download_args(tmp_path, force=False))

    assert first.read_bytes() == b"complete"
    assert second.read_bytes() == b"half-rest"
    assert not examples._partial_path(second).exists()
    assert calls[0] == b"half-"


def test_example_download_force_discards_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(examples.shutil, "which", lambda _name: "/usr/bin/curl")
    calls: list[bytes | None] = []
    monkeypatch.setattr(examples, "download_file", _fake_http_download(calls))
    example = examples._select_examples(["phase3-chrmt-vcf"], False)[0]
    example_dir = tmp_path / examples.COLLECTION_DIR / example.example_id
    example_dir.mkdir(parents=True)
    planned = examples._planned_downloads(example, example_dir, "https")
    destinations = [destination for _s, destination, _r, _m in planned]
    destinations[0].write_bytes(b"old")
    examples._partial_path(destinations[1]).write_bytes(b"stale-")

    examples.cmd_download(_download_args(tmp_path, force=True))

    assert calls == [None] * len(destinations)
    assert [d.read_bytes() for d in destinations] == [b"rest"] * len(destinations)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["info", "--input", "a.cram", "coverage-sample"], "a.cram"),
        (["--input", "a.cram", "info", "coverage-sample"], "a.cram"),
        (["info", "coverage-sample", "--input", "a.cram"], "a.cram"),
        (
            ["--input", "a.cram", "info", "--input", "b.cram", "coverage-sample"],
            "b.cram",
        ),
        (["info", "--input", "a.cram", "coverage-sample", "-i", "b.cram"], "b.cram"),
    ],
)
def test_shared_options_apply_at_every_subcommand_level(argv, expected):
    cli_parser, base_parser = parser.build_parser()
    args, explicit = parser.parse_args(cli_parser, base_parser, argv)
    assert args.input == expected
    assert "input" in explicit


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["info", "--input", "x.bam"], None),
        (["--config", "a.toml", "info"], "a.toml"),
        (["info", "--config=b.toml"], "b.toml"),
        (["--config", "a.toml", "info", "--config", "c.toml"], "c.toml"),
        (["info", "--", "--config", "d.toml"], None),
    ],
)
def test_config_path_from_argv(argv, expected):
    assert parser.config_path_from_argv(argv) == expected


def test_config_flag_reads_and_saves_only_that_file(tmp_path):
    own = tmp_path / "own.toml"
    own.write_text('output_directory = "/from/own/config"\n')
    user_config = config.get_config_path()
    try:
        config.set_config_path(own)
        config.reload_settings()
        assert config.settings.get("output_directory") == "/from/own/config"
        config.save_config({"reference_library": str(tmp_path / "ref")})
        assert config.config_args() == ["--config", str(own.resolve())]
    finally:
        config.set_config_path(None)
        config.reload_settings()
    assert "reference_library" in own.read_text()
    assert not user_config.exists()


def test_config_flag_reaches_the_cli(tmp_path):
    own = tmp_path / "own.toml"
    outdir = tmp_path / "from-own-config"
    own.write_text(f"output_directory = '{outdir}'\n")
    result = subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "--config", str(own), "config"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"Config File:      {own.resolve()}" in result.stdout
    assert "from-own-config" in result.stdout


def test_version_flag(capsys):
    from wgsextract_cli import __version__
    from wgsextract_cli.main import main

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"wgsextract {__version__}"
