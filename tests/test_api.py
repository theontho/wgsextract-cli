"""The typed Python API (wgsextract_cli.api, #346), called directly."""

import argparse
import dataclasses
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.htslib_helpers import needs_tools, write_fasta, write_read_pairs
from tests.smoke_utils import ensure_fake_data, run_cli
from wgsextract_cli import api
from wgsextract_cli.api._params import from_namespace, to_namespace
from wgsextract_cli.core import results
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.process_registry import proc_registry
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
            prop = schema["properties"][item.name]
            if prop["type"] in ("array", ["array", "null"]):  # lists of files
                prop = prop["items"]
            assert prop["type"] in ("string", ["string", "null"])
            assert prop["format"] == "path"


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


def _samtools_view(*args: str) -> list[str]:
    out = subprocess.run(["samtools", "view", *args], capture_output=True, text=True)
    return out.stdout.splitlines()


def test_bam_merge_takes_its_inputs_as_a_tuple_of_paths(fake_data, tmp_path):
    bam, _ref = fake_data
    pieces = []
    for contig in ("chrM", "chrY"):
        piece = tmp_path / f"{contig}.bam"
        subprocess.run(
            ["samtools", "view", "-b", "-o", str(piece), str(bam), contig], check=True
        )
        pieces.append(str(piece))
    params = api.BamMergeParams(inputs=pieces, output="merged.bam", outdir=tmp_path)  # type: ignore[arg-type]
    assert params.inputs == tuple(Path(p) for p in pieces)
    assert api.json_schema(api.BamMergeParams)["properties"]["inputs"]["type"] == (
        "array"
    )

    result = api.bam.merge(params)
    assert_valid(result)
    merged = tmp_path / "merged.bam"
    assert [o.role for o in result.outputs if o.path == str(merged)] == [
        "merged_alignment"
    ]
    expected = sum(len(_samtools_view(p)) for p in pieces)
    assert len(_samtools_view(str(merged))) == expected
    assert "SO:coordinate" in _samtools_view("-H", str(merged))[0]


def test_bam_sort_region_and_to_ubam(fake_data, tmp_path):
    bam, ref = fake_data
    sorted_result = api.bam.sort(
        api.BamSortParams(input=bam, ref=ref, outdir=tmp_path, region="chrM")
    )
    assert_valid(sorted_result)
    sorted_bam = tmp_path / "fake_sorted.bam"
    contigs = {line.split("\t")[2] for line in _samtools_view(str(sorted_bam))}
    assert contigs == {"chrM"}

    ubam = api.bam.to_ubam(
        api.BamToUbamParams(input=sorted_bam, outdir=tmp_path / "u", sample="S1")
    )
    assert_valid(ubam)
    out = Path(next(o.path for o in ubam.outputs if o.role == "unaligned_bam"))
    records = _samtools_view(str(out))
    assert records and all(line.split("\t")[2] == "*" for line in records)
    with pytest.raises(api.UsageError, match="--input .* or --r1"):
        api.BamToUbamParams()


def test_qc_vcf_reports_bcftools_stats(fake_data, tmp_path):
    if not shutil.which("bcftools"):
        pytest.skip("bcftools not found in PATH")
    vcf = FAKE_DIR / "fake.vcf.gz"
    # PR #442 review: an unused input (here a missing BAM) was still checked.
    unused = tmp_path / "unused.bam"
    params = api.QcVcfParams(input=unused, vcf_input=vcf, outdir=tmp_path)
    result = api.qc.vcf(params)
    assert_valid(result)
    assert [Path(i.path).name for i in result.inputs] == ["fake.vcf.gz"]
    stats = tmp_path / "fake.vcf.gz.vcfstats.txt"
    assert [o.role for o in result.outputs] == ["vcf_stats"]
    assert result.metrics["records"] > 0
    assert stats.read_text().startswith("# This file was produced by bcftools stats")
    with pytest.raises(api.UsageError, match="--input is required"):
        api.QcVcfParams()


def _vcf_records(vcf: Path, *args: str) -> list[str]:
    out = subprocess.run(
        ["bcftools", "view", "-H", *args, str(vcf)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


@needs_tools("bcftools", "tabix")
def test_vcf_filter_and_table_read_vcf_input(fake_data, tmp_path):
    vcf = FAKE_DIR / "fake.vcf.gz"
    unused = tmp_path / "unused.bam"
    filtered = api.vcf.filter(
        api.VcfFilterParams(
            input=unused, vcf_input=vcf, region="chr1", expr="QUAL>30", outdir=tmp_path
        )
    )
    assert_valid(filtered)
    assert [Path(i.path).name for i in filtered.inputs] == ["fake.vcf.gz"]
    out = tmp_path / "filtered.vcf.gz"
    expected = _vcf_records(vcf, "-r", "chr1", "-i", "QUAL>30")
    assert expected and _vcf_records(out) == expected

    table = api.vcf.table(
        api.VcfTableParams(input=out, outdir=tmp_path, no_sqlite=True)
    )
    assert_valid(table)
    (row_file,) = [o for o in table.outputs if o.role == "variant_table"]
    assert row_file.metrics["rows"] > 0
    assert not list(tmp_path.glob("*.sqlite"))


def test_vcf_confidence_thresholds_are_two_non_negative_numbers():
    vcf = Path("in.vcf.gz")
    params = api.VcfGnomadParams(input=vcf, confidence_depth=[5, 12.5])
    assert params.confidence_depth == (5, 12.5)
    with pytest.raises(api.UsageError, match="takes 2 values"):
        api.VcfClinvarParams(input=vcf, confidence_gq=(10,))
    for bad in (-1, float("nan")):
        with pytest.raises(api.UsageError, match="at least 0"):
            api.VcfPharmcatParams(input=vcf, confidence_depth=(bad, 20))
    # PR #452 review: crossed thresholds failed only after the annotation ran.
    with pytest.raises(api.UsageError, match="LOW <= HIGH"):
        api.VcfClinvarParams(input=vcf, confidence_gq=(30, 20))


@needs_tools("bwa", "samblaster", "samtools")
def test_align_then_realign_to_renamed_contigs(tmp_path):
    rng = random.Random(7)
    genome = "".join(rng.choice("ACGT") for _ in range(6_000))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ref = write_fasta(tmp_path / "a" / "ref.fa", {"one": genome})
    new_ref = write_fasta(tmp_path / "b" / "ref.fa", {"chr1": genome})
    r1, r2 = tmp_path / "s_R1.fastq", tmp_path / "s_R2.fastq"
    write_read_pairs(genome, r1, r2, rng.sample(range(len(genome) - 300), 100))

    aligned = api.align.align(
        api.AlignParams(
            r1=r1, r2=r2, ref=ref, aligner="bwa", sample="Ann", outdir=tmp_path / "a"
        )
    )
    assert_valid(aligned)
    assert [(Path(i.path).name, i.role) for i in aligned.inputs] == [
        ("s_R1.fastq", "fastq_r1"),
        ("s_R2.fastq", "fastq_r2"),
    ]
    bam = _output(aligned, "alignment")
    assert bam == tmp_path / "a" / "s_aligned.bam"
    assert "\tSM:Ann" in "".join(_samtools_view("-H", str(bam)))
    assert len(_samtools_view(str(bam))) == 200

    params = api.RealignParams(
        input=bam, ref=new_ref, aligner="bwa", outdir=tmp_path / "b", no_preflight=True
    )
    realigned = api.align.realign(params)
    assert_valid(realigned)
    out = _output(realigned, "alignment")
    assert out == tmp_path / "b" / "s_aligned_aligned.bam"
    records = [line.split("\t") for line in _samtools_view(str(out))]
    assert len(records) == 200 and {r[2] for r in records} == {"chr1"}
    assert _output(realigned, "step_journal").is_file()

    # A dry run of a finished realign changes nothing.
    before = out.stat().st_mtime_ns
    dry = api.align.realign(dataclasses.replace(params, dry_run=True))
    assert dry.status == "ok" and out.stat().st_mtime_ns == before


def test_analyze_batch_gen_then_a_quiet_dry_run_of_the_batch(
    fake_data, tmp_path, capfd, monkeypatch
):
    _bam, ref = fake_data
    scan = tmp_path / "scan"
    scan.mkdir()
    for name, copy in (("fake.cram", "ann.cram"), ("fake.vcf.gz", "ann_snp.vcf.gz")):
        for suffix in ("", ".crai" if name.endswith("cram") else ".tbi"):
            shutil.copy(FAKE_DIR / f"{name}{suffix}", scan / f"{copy}{suffix}")
    batch = tmp_path / "batch.csv"

    made = api.analyze.batch_gen(
        api.AnalyzeBatchGenParams(directory=scan, output=batch)
    )
    assert _output(made, "batch_file") == batch and made.metrics["samples"] == 1
    params = api.AnalyzeComprehensiveParams(
        batch=batch, ref=ref, outdir=tmp_path / "out", dry_run=True
    )
    result = api.analyze.comprehensive(params)

    assert_valid(result)
    assert (result.metrics["samples"], result.metrics["failed_samples"]) == (1, 0)
    assert not list((tmp_path / "out" / "ann").iterdir())  # a dry run saves nothing
    # The workflow's stage lines aren't printed on the API caller's stdout.
    assert capfd.readouterr().out == ""
    # PR #463 review: with no stdout at all (pythonw) it failed on the first.
    monkeypatch.setattr(sys, "stdout", None)
    assert api.analyze.comprehensive(params).status == "ok"


def test_analyze_rejects_crossed_config_thresholds_before_any_work(
    fake_data, tmp_path, monkeypatch
):
    # PR #463 review: Params built before config.toml set crossed thresholds
    # were caught only after the workflow's expensive steps.
    _bam, ref = fake_data
    params = api.AnalyzeComprehensiveParams(
        vcf_inputs=(FAKE_DIR / "fake.vcf.gz",), ref=ref, outdir=tmp_path / "out"
    )
    monkeypatch.setitem(settings, "confidence_min_depth", 30)
    monkeypatch.setitem(settings, "confidence_high_depth", 20)
    with pytest.raises(api.CommandFailed, match="depth 30/20") as failed:
        api.analyze.comprehensive(params)
    assert failed.value.result.exit_code == 2  # a usage error
    assert not (tmp_path / "out" / "analyze.steps.json").exists()  # no step ran


def _output(result: api.Result, role: str) -> Path:
    return Path(next(o.path for o in result.outputs if o.role == role))


def test_each_force_step_is_checked():
    with pytest.raises(api.UsageError, match="--force-step must be one of .*'sort'"):
        api.RealignParams(input=Path("x.bam"), force_steps=("align", "sort"))


def test_align_without_reads_is_refused_before_it_runs():
    # PR #445 review: a server job without r1 was accepted and then failed late.
    with pytest.raises(api.UsageError, match="--r1 is required"):
        api.AlignParams(ref=Path("ref.fa"))


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
