import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools
from wgsextract_cli.core import bwa_index, config
from wgsextract_cli.core.atomic_output import atomic_output
from wgsextract_cli.core.file_lock import build_lock
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.main import main


def test_atomic_writer_failure_keeps_previous_output_and_index(tmp_path):
    output = tmp_path / "calls.vcf.gz"
    index = Path(str(output) + ".tbi")
    output.write_bytes(b"valid previous output")
    index.write_bytes(b"previous index")
    with pytest.raises(RuntimeError), atomic_output(output) as staged:
        Path(staged).write_bytes(b"interrupted output")
        raise RuntimeError("interrupted")
    assert output.read_bytes() == b"valid previous output"
    assert index.read_bytes() == b"previous index"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "calls.vcf.gz",
        "calls.vcf.gz.tbi",
    ]
    with atomic_output(output) as staged:
        Path(staged).write_bytes(b"completed output")
    assert output.read_bytes() == b"completed output"
    assert not index.exists()


def test_atomic_mode_still_cleans_partial_outputs_from_direct_writers(tmp_path):
    from wgsextract_cli.commands._extract_helpers import cleanup_files
    from wgsextract_cli.core.atomic_output import atomic_output_mode

    output = tmp_path / "partial.bam"
    temporary = tmp_path / "spill.bam"
    with (
        atomic_output_mode(True),
        pytest.raises(RuntimeError),
        cleanup_files(temp=[temporary], on_failure=[output]),
    ):
        output.write_bytes(b"partial direct output")
        temporary.write_bytes(b"spill")
        raise RuntimeError("writer interrupted")
    assert not output.exists() and not temporary.exists()


@pytest.mark.parametrize(
    ("command", "explicit"), [("align", "r1"), ("vcf", "vcf_input")]
)
def test_alternative_input_does_not_validate_unmounted_configured_bam(
    tmp_path, monkeypatch, command, explicit
):
    from argparse import Namespace

    from wgsextract_cli.cli import dispatch
    from wgsextract_cli.core import alignment_input

    called = []
    args = Namespace(
        command=command,
        input=str(tmp_path / "unmounted" / "default.bam"),
        outdir=None,
        debug=False,
        atomic_output=False,
        keep_awake=False,
        func=lambda parsed: called.append(parsed),
    )
    monkeypatch.setattr(dispatch, "verify_platform_support", lambda parsed: None)
    monkeypatch.setattr(dispatch, "apply_genome_selection", lambda *args: None)
    monkeypatch.setattr(
        alignment_input,
        "validate_alignment_input",
        lambda path: pytest.fail("unused configured input was validated"),
    )
    dispatch.dispatch_command(args, {explicit})
    assert called == [args]


def test_publication_failure_restores_the_previous_index(tmp_path, monkeypatch):
    output = tmp_path / "alignment.bam"
    index = Path(str(output) + ".bai")
    output.write_bytes(b"previous BAM")
    index.write_bytes(b"previous index")
    alternate = output.with_suffix(".bai")
    alternate.write_bytes(b"alternate index")
    replace = os.replace

    def fail_output_publication(source, target):
        if Path(target) == output:
            raise PermissionError("output is busy")
        replace(source, target)

    monkeypatch.setattr(os, "replace", fail_output_publication)
    with pytest.raises(PermissionError), atomic_output(output) as staged:
        Path(staged).write_bytes(b"new BAM")
    assert output.read_bytes() == b"previous BAM"
    assert index.read_bytes() == b"previous index"
    assert alternate.read_bytes() == b"alternate index"
    assert set(tmp_path.iterdir()) == {output, index, alternate}


def test_atomic_alignment_retires_basename_index_and_config_leaves_neighbors(tmp_path):
    from wgsextract_cli.core.atomic_output import atomic_write

    output = tmp_path / "alignment.bam"
    alternate = output.with_suffix(".bai")
    alternate.write_bytes(b"stale index")
    with atomic_output(output) as staged:
        Path(staged).write_bytes(b"new BAM")
    assert not alternate.exists()
    config_file = tmp_path / "config.toml"
    neighbor = Path(str(config_file) + ".idx")
    neighbor.write_bytes(b"unrelated data")
    atomic_write(config_file, b"cpu_threads = 1\n")
    assert neighbor.read_bytes() == b"unrelated data"


@pytest.mark.parametrize("backend", ["parabricks", "ultima"])
def test_atomic_special_alignment_failure_preserves_the_existing_bam(
    tmp_path, monkeypatch, backend
):
    from argparse import Namespace

    from wgsextract_cli.commands import align
    from wgsextract_cli.core import parabricks
    from wgsextract_cli.core.atomic_output import atomic_output_mode

    output = tmp_path / "sample_aligned.bam"
    output.write_bytes(b"previous BAM")
    setup = align._AlignSetup("1", "256M", str(tmp_path), "reference", "sample")
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(align, "run_pipeline", lambda *args, **kwargs: None)
    monkeypatch.setattr(parabricks, "host_path", str)

    def interrupted(command, *args, **kwargs):
        option = "--out-bam" if backend == "parabricks" else "-O"
        argv = args[0] if backend == "parabricks" else command
        Path(argv[argv.index(option) + 1]).write_bytes(b"truncated BAM")
        raise RuntimeError("interrupted")

    monkeypatch.setattr(parabricks, "run_pbrun", interrupted)
    monkeypatch.setattr(align, "run_command", interrupted)
    with atomic_output_mode(True), pytest.raises(RuntimeError):
        if backend == "parabricks":
            align._gpu_align(
                Namespace(format="BAM", r1="reads"), setup, "fq2bam", [], []
            )
        else:
            align._sort_and_mark_ultima([], str(output), setup, "BAM", str(tmp_path))
    assert output.read_bytes() == b"previous BAM"
    assert set(tmp_path.iterdir()) == {output}


def test_live_builder_is_not_evicted_by_a_wait_timeout(tmp_path):
    lock = str(tmp_path / "index.lock")
    with build_lock(lock), pytest.raises(WGSExtractError, match="Timed out"):
        with build_lock(lock, timeout=0.01):
            pytest.fail("a live builder was evicted")
    assert not Path(lock).exists()


def test_dead_builder_lock_is_reclaimed(tmp_path):
    lock = tmp_path / "index.lock"
    lock.write_text(
        json.dumps({"pid": 2147483647, "created": 0, "host": socket.gethostname()})
    )
    with build_lock(str(lock), timeout=0.01):
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert not lock.exists()


def test_concurrent_stale_lock_reclaimers_never_evict_a_new_owner(
    tmp_path, monkeypatch
):
    from wgsextract_cli.core import file_lock

    lock = tmp_path / "index.lock"
    lock.write_text(
        json.dumps({"pid": 2147483647, "created": 0, "host": socket.gethostname()})
    )
    stale = threading.Barrier(2)
    first_stale_read = threading.Event()
    first_entered = threading.Event()
    second_entered = threading.Event()
    abandoned = file_lock._abandoned
    unlink = Path.unlink
    active = 0
    overlaps = []
    errors = []

    def simultaneous_stale_read(path, timeout):
        result = abandoned(path, timeout)
        if result:
            if threading.current_thread().name == "first":
                first_stale_read.set()
            try:
                stale.wait(0.1)
            except threading.BrokenBarrierError:
                pass
        return result

    def delayed_unlink(path, **kwargs):
        if path == lock and threading.current_thread().name == "second":
            assert first_entered.wait(2)
        return unlink(path, **kwargs)

    def builder():
        nonlocal active
        try:
            with build_lock(str(lock), timeout=2):
                active += 1
                overlaps.append(active)
                if threading.current_thread().name == "first":
                    first_entered.set()
                    second_entered.wait(0.2)
                else:
                    second_entered.set()
                active -= 1
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(file_lock, "_abandoned", simultaneous_stale_read)
    monkeypatch.setattr(Path, "unlink", delayed_unlink)
    threads = [
        threading.Thread(target=builder, name=name) for name in ("first", "second")
    ]
    threads[0].start()
    assert first_stale_read.wait(2)
    threads[1].start()
    for thread in threads:
        thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert overlaps == [1, 1]


@needs_tools("bwa")
def test_interrupted_bwa_build_is_not_trusted_and_next_run_rebuilds(tmp_path):
    reference = tmp_path / "tiny.fa"
    reference.write_text(">chrTiny\nACGTACGTACGT\n")
    ref = str(reference)
    # This was the old success criterion, even though the rest is missing.
    Path(ref + ".bwt").write_text("incomplete")
    assert not bwa_index.has_index(ref, bwa_index.INDEX_SUFFIXES, "bwa")

    def interrupted(command):
        Path(command[3] + ".bwt").write_text("partial staged index")
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError):
        bwa_index.ensure_index(ref, runner=interrupted)
    assert not bwa_index.has_index(ref, bwa_index.INDEX_SUFFIXES, "bwa")
    assert Path(ref + ".bwt").read_text() == "incomplete"
    bwa_index.ensure_index(ref)
    assert bwa_index.has_index(ref, bwa_index.INDEX_SUFFIXES, "bwa")
    assert Path(ref + ".bwa.complete").is_file()
    assert not list(tmp_path.glob(".tiny.fa.bwa-index-*"))


@pytest.mark.parametrize("command", [["info"], ["vcf", "snp"], ["microarray"]])
def test_bad_alignment_fails_cleanly_before_inference(tmp_path, command, capsys):
    bad = tmp_path / "bad.bam"
    bad.write_bytes(b"not a BAM")
    with pytest.raises(SystemExit) as error:
        main([*command, "--input", str(bad), "--config", str(tmp_path / "config.toml")])
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "Invalid alignment input" in message
    assert "unexpected error" not in message.lower()
    assert "CalledProcessError" not in message
    assert "called as for a woman" not in message


def test_atomic_alignment_failure_keeps_the_previous_bam(tmp_path, monkeypatch):
    from wgsextract_cli.commands import align
    from wgsextract_cli.core.atomic_output import atomic_output_mode

    output = tmp_path / "alignment.bam"
    output.write_bytes(b"previous valid BAM")
    index = Path(str(output) + ".bai")
    index.write_bytes(b"previous index")

    def interrupted(stages, **kwargs):
        sort = stages[-1]
        Path(sort[sort.index("-o") + 1]).write_bytes(b"interrupted BAM")
        raise RuntimeError("interrupted alignment")

    monkeypatch.setattr(align, "run_pipeline", interrupted)
    with atomic_output_mode(True), pytest.raises(RuntimeError):
        align.run_streamed_alignment(
            ["bwa", "mem", "reference", "reads"],
            str(output),
            threads="1",
            memory="256M",
            fmt="BAM",
            ref="reference",
            duplicates=None,
        )
    assert output.read_bytes() == b"previous valid BAM"
    assert index.read_bytes() == b"previous index"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "alignment.bam",
        "alignment.bam.bai",
    ]


@pytest.mark.parametrize("extension", ["bam", "cram"])
def test_failed_atomic_alignment_index_never_publishes_partial_sidecar(
    tmp_path, monkeypatch, extension
):
    from wgsextract_cli.commands import align
    from wgsextract_cli.core.atomic_output import atomic_output_mode

    output = tmp_path / f"alignment.{extension}"
    output.write_bytes(b"completed alignment")
    suffix = ".crai" if extension == "cram" else ".bai"
    index = Path(str(output) + suffix)
    index.write_bytes(b"previous valid index")

    def interrupted(command):
        Path(command[-1]).write_bytes(b"partial index")
        raise RuntimeError("interrupted indexing")

    monkeypatch.setattr(align, "run_command", interrupted)
    with atomic_output_mode(True), pytest.raises(RuntimeError):
        align._index_output(str(output))
    assert index.read_bytes() == b"previous valid index"
    assert set(tmp_path.iterdir()) == {output, index}


def test_tool_failure_is_clean_and_keeps_recovery_attributes():
    with pytest.raises(
        WGSExtractError, match=r"failed \(exit 7\): useful diagnostic"
    ) as error:
        run_command(
            [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('useful diagnostic');sys.exit(7)",
            ]
        )
    assert isinstance(error.value, subprocess.CalledProcessError)
    assert error.value.returncode == 7


def test_tool_stderr_progress_is_visible_before_completion(tmp_path, monkeypatch):
    import io

    progress = threading.Event()
    acknowledge = tmp_path / "acknowledge"
    errors = []

    class Listener(io.StringIO):
        def write(self, text):
            if "progress" in text:
                progress.set()
            return super().write(text)

    monkeypatch.setattr(sys, "stderr", Listener())

    def execute():
        try:
            run_command(
                [
                    sys.executable,
                    "-c",
                    "import sys,time;from pathlib import Path;"
                    "sys.stderr.write('progress\\n');sys.stderr.flush();"
                    f"ack=Path({str(acknowledge)!r});deadline=time.monotonic()+20;"
                    "\nwhile not ack.exists() and time.monotonic()<deadline: time.sleep(.01)\n",
                ]
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert progress.wait(10)
        assert thread.is_alive()
    finally:
        acknowledge.touch()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors


def test_config_prompt_never_reads_a_noninteractive_pipe(monkeypatch, tmp_path, capsys):
    from wgsextract_cli.cli.parser import bootstrap_default_config

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("blocking prompt"))
    bootstrap_default_config(tmp_path / "config.toml")
    assert "in a terminal" in capsys.readouterr().out


def test_config_save_interruption_preserves_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("cpu_threads = 2\n")
    monkeypatch.setattr(config, "get_config_path", lambda: path)

    def interrupted(*args):
        raise OSError("interrupted publication")

    monkeypatch.setattr("wgsextract_cli.core.atomic_output.os.replace", interrupted)
    with pytest.raises(OSError):
        config.save_config({"cpu_threads": 4})
    assert path.read_text() == "cpu_threads = 2\n"
    assert list(tmp_path.iterdir()) == [path]


def test_unknown_config_key_warns_once_with_a_suggestion(monkeypatch, tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text("refernce_library = 'reference'\n")
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    config.load_config()
    config.load_config()
    assert caplog.text.count("Unknown setting 'refernce_library'") == 1
    assert "Did you mean 'reference_library'?" in caplog.text
