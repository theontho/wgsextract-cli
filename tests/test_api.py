"""The typed Python API (wgsextract_cli.api, #346), called directly."""

import argparse
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.smoke_utils import ensure_fake_data, run_cli
from wgsextract_cli import api
from wgsextract_cli.api._params import from_namespace, to_namespace
from wgsextract_cli.core import results
from wgsextract_cli.core.config import settings

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
    assert schema["properties"]["input"]["type"] in ("string", ["string", "null"])


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
