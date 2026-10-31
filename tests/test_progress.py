import logging
import sys
import time

from wgsextract_cli.core import utils
from wgsextract_cli.core.progress import Heartbeat


def test_pipeline_heartbeat_preserves_binary_data_and_counts_stderr(
    tmp_path, caplog, monkeypatch
):
    original = utils.Heartbeat
    monkeypatch.setattr(
        utils, "Heartbeat", lambda *a, **k: original(*a, interval=0.01, **k)
    )
    stages = [
        [
            sys.executable,
            "-c",
            "import sys,time;sys.stderr.write('Processed 2 reads in 1s\\n');"
            "sys.stderr.flush();sys.stdout.buffer.write(b'\\x00data\\xff');time.sleep(.06)",
            "bwa",
        ]
    ]
    caplog.set_level(logging.INFO)
    for enabled in (False, True):
        with (tmp_path / str(enabled)).open("wb") as stream:
            utils.run_pipeline(
                stages, stdout=stream, heartbeat=enabled, expected_reads=4
            )
    assert (
        (tmp_path / "False").read_bytes()
        == (tmp_path / "True").read_bytes()
        == b"\x00data\xff"
    )
    assert "2/4 reads (50.0%)" in caplog.text
    assert "estimated" in caplog.text


def test_heartbeat_reports_while_no_shards_complete_and_stops(caplog):
    caplog.set_level(logging.INFO)
    with Heartbeat("GATK scatter", 2, "shards", interval=0.01) as progress:
        time.sleep(0.04)
        progress.advance()
        progress.report()
    assert caplog.text.count("0/2 shards") >= 2
    assert "1/2 shards" in caplog.text
    assert not progress._thread.is_alive()


def test_progress_scan_error_cannot_fail_the_data_pipeline(caplog):
    def unavailable():
        raise OSError("monitor unavailable")

    caplog.set_level(logging.INFO)
    with Heartbeat("Extraction", 10, scan=unavailable, interval=0.01):
        time.sleep(0.03)
    assert "Progress scan unavailable" in caplog.text
