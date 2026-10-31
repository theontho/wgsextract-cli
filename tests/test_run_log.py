import argparse
import json
import logging
import sys
from threading import Event

import pytest

from wgsextract_cli.cli.bootstrap import configure_logging
from wgsextract_cli.commands._extract_helpers import cleanup_files
from wgsextract_cli.commands.vcf.gatk_scatter import _run_worker
from wgsextract_cli.core import dependency_checks
from wgsextract_cli.core.run_log import RunLogError, run_logging
from wgsextract_cli.core.utils import WGSExtractError, run_command


def test_opt_in_log_preserves_stdout_quiet_and_provenance(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        dependency_checks, "get_tool_version", lambda tool: "test-version"
    )
    path = tmp_path / "run.log"
    args = argparse.Namespace(log_file=str(path), log_dir=None, ref=None)
    old_handlers = logging.getLogger().handlers[:]
    old_level = logging.getLogger().level
    try:
        configure_logging(quiet=True)
        with run_logging(args, ["info", "--log-file", str(path)]):
            assert (
                json.loads((tmp_path / "run.log.provenance.json").read_text())["status"]
                == "running"
            )
            logging.info("diagnostic only in file")
            result = run_command(
                [sys.executable, "-c", "print('data')"], capture_output=True
            )
        assert result.stdout == "data\n"
        assert "diagnostic only in file" not in capsys.readouterr().err
        assert "diagnostic only in file" in path.read_text()
        metadata = json.loads((tmp_path / "run.log.provenance.json").read_text())
        assert metadata["status"] == "success"
        assert metadata["commands"][0][0] == sys.executable
        assert metadata["tool_versions"]
        assert metadata["argv"][:1] == ["info"]
    finally:
        logging.getLogger().handlers = old_handlers
        logging.getLogger().setLevel(old_level)


def test_failed_run_retains_failure_log_and_cannot_truncate_old_run(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dependency_checks, "get_tool_version", lambda tool: None)
    path = tmp_path / "run.log"
    args = argparse.Namespace(log_file=str(path), log_dir=None, ref=None)
    with pytest.raises(RuntimeError, match="failed"):
        with run_logging(args, ["align"]):
            logging.error("failure diagnostic")
            raise RuntimeError("failed")
    before = path.read_bytes()
    assert (
        json.loads((tmp_path / "run.log.provenance.json").read_text())["status"]
        == "failed"
    )
    with pytest.raises(RunLogError):
        with run_logging(args, ["align"]):
            pass
    assert path.read_bytes() == before


def test_log_directory_generates_unique_runs_and_rejects_output_collision(tmp_path):
    args = argparse.Namespace(log_file=None, log_dir=str(tmp_path), ref=None)
    for _ in range(2):
        with run_logging(args, ["info"]):
            pass
    assert len(list(tmp_path.glob("*.log"))) == 2
    collision = tmp_path / "collision.log"
    args.log_file = str(collision)
    with pytest.raises(WGSExtractError, match="collides"):
        with run_logging(args, ["align"]):
            with cleanup_files(on_failure=[collision]):
                run_command([sys.executable, "-c", "print('unused')", str(collision)])
    assert "unused" not in collision.read_text()


def test_gatk_worker_retains_tool_diagnostics_without_capturing_data(tmp_path):
    path = tmp_path / "gatk.log"
    args = argparse.Namespace(log_file=str(path), log_dir=None, ref=None)
    with run_logging(args, ["vcf", "gatk"]):
        _run_worker(
            [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('native kernel diagnostic\\n')",
            ],
            tmp_path / "shard.log",
            Event(),
        )
    assert "native kernel diagnostic" in path.read_text()
