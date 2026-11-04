"""The typed Python API (wgsextract_cli.api, #346), called directly."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.smoke_utils import ensure_fake_data, run_cli
from wgsextract_cli import api
from wgsextract_cli.api._params import from_namespace, to_namespace
from wgsextract_cli.core import results
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.process_registry import cancel_on_event, proc_registry
from wgsextract_cli.core.utils import child_stdout_to_stderr, popen, run_command

CLI_ROOT = Path(__file__).resolve().parents[1]
FAKE_DIR = CLI_ROOT / "out" / "fake_30x"
VALIDATOR = Draft202012Validator(results.load_schema())

PARAMS_CLASSES = [
    getattr(api, name)
    for name in api.__all__
    if name.endswith("Params") and name != "Params"
]


@pytest.fixture
def fake_data() -> tuple[Path, Path]:
    if not shutil.which("samtools"):
        pytest.skip("samtools not found in PATH")
    ensure_fake_data(str(FAKE_DIR))
    return FAKE_DIR / "fake.bam", FAKE_DIR / "fake_ref.fa"


def test_params_coerce_paths_and_validate():
    params = api.InfoParams(input="a.bam", ref="")  # type: ignore[arg-type]
    assert params.input == Path("a.bam")
    assert params.ref is None
    with pytest.raises(api.UsageError, match="--input required"):
        api.InfoParams(input=None)  # type: ignore[arg-type]
    with pytest.raises(api.UsageError, match="panel"):
        api.YStrParams(input=Path("a.bam"), panel="nope")
    with pytest.raises(api.UsageError):
        api.VcfSnpParams(input=Path("a.bam"), ploidy="1", ploidy_file=Path("p.txt"))
    with pytest.raises(api.UsageError, match="microarray raw-data file"):
        api.ChipParams()
    assert api.ChipParams(list_revisions=True).input is None
    with pytest.raises(AttributeError):
        params.detailed = True  # type: ignore[misc]


def test_namespace_round_trip():
    params = api.MicroarrayParams(
        input=Path("a.cram"),
        formats="23andme_v5",
        indel_manifest=("m1.tsv", "m2.tsv"),
        parallel=False,
    )
    namespace = to_namespace(params)
    assert namespace.indel_manifest == ["m1.tsv", "m2.tsv"]
    assert namespace.input == "a.cram"
    assert from_namespace(api.MicroarrayParams, namespace) == params
    # Attributes the namespace lacks take the field defaults.
    assert from_namespace(
        api.InfoParams, argparse.Namespace(input="a.bam")
    ) == api.InfoParams(input=Path("a.bam"))


def test_config_fills_unset_fields_only(fake_data):
    bam, ref = fake_data
    settings["reference_fasta"] = str(ref)
    seen: list[api.InfoParams] = []
    api._runtime.run_inline(api.InfoParams(input=bam), seen.append)
    assert seen[0].ref == ref
    seen.clear()
    api._runtime.run_inline(api.InfoParams(input=bam, ref=Path("x.fa")), seen.append)
    assert seen[0].ref == Path("x.fa")


@pytest.mark.parametrize("cls", PARAMS_CLASSES, ids=lambda c: c.__name__)
def test_json_schema_describes_every_field(cls):
    schema = api.json_schema(cls)
    Draft202012Validator.check_schema(schema)
    assert set(schema["properties"]) == {f.name for f in api._params.param_fields(cls)}
    assert all(p.get("description") for p in schema["properties"].values())
    for item in api._params.param_fields(cls):
        if item.spec.path:
            assert schema["properties"][item.name]["type"] in (
                "string",
                ["string", "null"],
            )


def test_json_schema_has_choices_and_required():
    schema = api.json_schema(api.YStrParams)
    assert "input" in schema["required"]
    assert "ref" not in schema["required"]
    assert schema["properties"]["panel"]["enum"]


def test_chip_list_is_quiet_and_structured(capsys):
    result = api.info.chip(api.ChipParams(list_revisions=True))
    assert result.status == "ok" and result.exit_code == 0
    assert result.command == "info chip"
    assert result.details["revisions"]
    assert result.provenance.params["list_revisions"] is True
    assert capsys.readouterr().out == ""
    assert_valid(result)


def test_cancelled_before_start():
    token = api.CancelToken()
    token.cancel()
    result = api.info.chip(
        api.ChipParams(list_revisions=True), cancel=token, check=False
    )
    assert (result.status, result.exit_code) == ("cancelled", 130)
    with pytest.raises(api.CommandFailed) as raised:
        api.info.chip(api.ChipParams(list_revisions=True), cancel=token)
    assert raised.value.exit_code == 130
    assert raised.value.result.error is not None


def test_cancel_token_callbacks():
    token = api.CancelToken()
    calls: list[str] = []
    unregister = token.on_cancel(lambda: calls.append("a"))
    token.on_cancel(lambda: calls.append("b"))
    unregister()
    token.cancel()
    token.cancel()
    assert calls == ["b"] and token.is_set()
    token.on_cancel(lambda: calls.append("late"))
    assert calls == ["b", "late"]
    with pytest.raises(api.Cancelled):
        token.raise_if_cancelled()


def test_failure_raises_with_the_result(tmp_path):
    with pytest.raises(api.CommandFailed) as raised:
        api.info.info(api.InfoParams(input=tmp_path / "missing.bam", outdir=tmp_path))
    result = raised.value.result
    assert result.status == "failed" and result.exit_code == raised.value.exit_code
    assert result.error is not None and "missing.bam" in result.error.message
    assert_valid(result)


def test_info_matches_the_cli(fake_data, tmp_path, capsys):
    bam, ref = fake_data
    events: list[api.ProgressEvent] = []
    result = api.info.info(
        api.InfoParams(input=bam, ref=ref, outdir=tmp_path / "api"),
        progress=events.append,
    )
    assert capsys.readouterr().out == ""
    assert_valid(result)
    assert events[0].stage == "start" and events[-1] == api.ProgressEvent(
        "finish", "info", 1.0
    )
    assert any(e.stage == "tool" and "samtools" in e.message for e in events)
    assert result.metrics["reference_model"]

    rc, stdout, stderr = run_cli(
        ["info", "--input", str(bam), "--ref", str(ref)]
        + ["--outdir", str(tmp_path / "cli"), "--json"]
    )
    assert rc == 0, stderr
    cli = json.loads(stdout)
    assert cli["command"] == result.command
    assert cli["metrics"] == json.loads(json.dumps(result.metrics))
    assert cli["details"] == json.loads(json.dumps(result.details))
    assert [o["role"] for o in cli["outputs"]] == [o.role for o in result.outputs]


def test_cancel_while_tools_run(fake_data, tmp_path):
    bam, ref = fake_data
    token = api.CancelToken()

    def cancel_at_first_tool(event: api.ProgressEvent) -> None:
        if event.stage == "tool":
            token.cancel()

    result = api.info.info(
        api.InfoParams(input=bam, ref=ref, outdir=tmp_path),
        progress=cancel_at_first_tool,
        cancel=token,
        check=False,
    )
    assert (result.status, result.exit_code) == ("cancelled", 130)
    # Cancelled from the tool's own progress event, so the tool never started.
    assert not result.provenance.tools


def test_progress_output_reaches_stdout_but_tools_do_not(fake_data, tmp_path, capfd):
    """The caller's own writes during a call stay on stdout (a stdio JSON-RPC
    server's channel); only the human text and external tools' stdout do not."""
    bam, ref = fake_data
    api.info.info(
        api.InfoParams(input=bam, ref=ref, outdir=tmp_path),
        progress=lambda event: print(f"progress:{event.stage}", flush=True),
    )
    out = capfd.readouterr().out
    assert out.splitlines()[0] == "progress:start"
    assert out.splitlines()[-1] == "progress:finish"
    assert all(line.startswith("progress:") for line in out.splitlines())

    with child_stdout_to_stderr():
        run_command([sys.executable, "-c", "print('from a tool')"])
    captured = capfd.readouterr()
    assert "from a tool" in captured.err and "from a tool" not in captured.out


def test_runs_without_stdout_or_stderr(fake_data, tmp_path, monkeypatch):
    """pythonw and windowed apps have sys.stdout and sys.stderr set to None."""
    bam, ref = fake_data
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    result = api.info.info(api.InfoParams(input=bam, ref=ref, outdir=tmp_path))
    assert result.status == "ok"
    with results.stdout_reserved() as real:
        print("dropped")
        real.write("dropped too")


def test_explicit_auto_threads_beat_config(monkeypatch):
    """`--threads auto` (None) must not be replaced by config cpu_threads."""
    from wgsextract_cli.api._cli import cli_handler

    monkeypatch.setitem(settings, "cpu_threads", 4)
    seen: list[api.VcfSnpParams] = []
    handler = cli_handler(api.VcfSnpParams, seen.append)
    for typed, expected in (({"threads"}, None), (set(), 4)):
        args = argparse.Namespace(input="x.bam", threads=None, _explicit_dests=typed)
        handler(args)
        assert seen.pop().threads == expected


def test_config_is_loaded_on_first_call(monkeypatch, tmp_path):
    from wgsextract_cli.core import config

    path = tmp_path / "config.toml"
    path.write_text("cpu_threads = 3\n")
    monkeypatch.setattr(config, "_config_path_override", path)
    monkeypatch.setattr(config, "_settings_loaded", False)
    saved = dict(settings)
    settings.clear()
    try:
        api.info.chip(api.ChipParams(list_revisions=True))
        assert settings["cpu_threads"] == 3
    finally:
        settings.clear()
        settings.update(saved)


def test_tools_started_after_cancel_are_stopped():
    proc_registry.cancel()
    try:
        process = popen([sys.executable, "-c", "import time; time.sleep(60)"])
        assert process.wait(timeout=10) != 0
    finally:
        proc_registry.accept_new()
    process = popen([sys.executable, "-c", "pass"])
    assert process.wait(timeout=10) == 0


def test_cancel_on_event_stops_a_pool_workers_tools():
    import threading

    event = threading.Event()
    cancel_on_event(event)
    process = popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        event.set()
        assert process.wait(timeout=10) != 0
    finally:
        proc_registry.accept_new()


def _child_context() -> tuple[bool, bool]:
    from wgsextract_cli.api import _runtime

    return _runtime._context.progress is None, _runtime._context.cancel is None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_forked_workers_do_not_inherit_the_callers_context():
    import multiprocessing

    from wgsextract_cli.api import _runtime

    _runtime._context.progress = lambda event: None
    _runtime._context.cancel = api.CancelToken()
    try:
        with multiprocessing.get_context("fork").Pool(1) as pool:
            assert pool.apply(_child_context) == (True, True)
    finally:
        _runtime._context.progress = _runtime._context.cancel = None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions")
def test_interrupted_run_command_stops_its_tool(monkeypatch):
    started: list[subprocess.Popen[str]] = []

    def interrupted(self, *args, **kwargs):
        started.append(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_command([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.undo()
    assert started[0].wait(timeout=10) != 0


def test_vcf_snp_on_chrm(fake_data, tmp_path):
    if not shutil.which("bcftools"):
        pytest.skip("bcftools not found in PATH")
    bam, ref = fake_data
    result = api.vcf.snp(
        api.VcfSnpParams(input=bam, ref=ref, outdir=tmp_path, region="chrM", ploidy="1")
    )
    assert_valid(result)
    vcfs = [o for o in result.outputs if o.path.endswith(".vcf.gz")]
    assert vcfs and Path(vcfs[0].path).exists()
    assert "bcftools" in result.provenance.tools
    assert result.manifest and Path(result.manifest).exists()


def assert_valid(result: api.Result) -> None:
    document = json.loads(json.dumps(result.to_dict()))
    errors = sorted(VALIDATOR.iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_api_docs_are_up_to_date():
    import subprocess
    import sys

    script = CLI_ROOT / "scripts" / "gen_api_docs.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--check"], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
