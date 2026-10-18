"""Regression tests for CLI bootstrap, config, and core runtime helpers."""

import logging
import subprocess
import threading
from argparse import Namespace
from pathlib import Path

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
    proc = subprocess.Popen(["sleep", "30"], **process_registry.process_group_kwargs())
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


def _fake_curl(calls):
    def run(cmd, **_kwargs):
        output = Path(cmd[cmd.index("--output") + 1])
        calls.append(output.read_bytes() if output.exists() else None)
        with output.open("ab") as f:
            f.write(b"rest")

    return run


def test_example_download_resumes_partial_and_skips_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(examples.shutil, "which", lambda _name: "/usr/bin/curl")
    calls: list[bytes | None] = []
    monkeypatch.setattr(examples, "run_command", _fake_curl(calls))
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
    monkeypatch.setattr(examples, "run_command", _fake_curl(calls))
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
