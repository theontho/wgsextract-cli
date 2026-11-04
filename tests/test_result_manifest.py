"""The wgsextract.result/1 manifest, the --json switch and exit codes (#345)."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.smoke_utils import ensure_fake_data, run_cli
from wgsextract_cli.cli.dispatch import dispatch_command
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.core import results
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
)

CLI_ROOT = Path(__file__).resolve().parents[1]
FAKE_DIR = CLI_ROOT / "out" / "fake_30x"
SCHEMA = results.load_schema()
VALIDATOR = Draft202012Validator(SCHEMA)


def assert_valid(document: dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def dispatch(func, tmp_path: Path, *, as_json: bool = False, **extra) -> int:
    args = argparse.Namespace(
        func=func,
        command="test",
        _command="test cmd",
        outdir=str(tmp_path),
        json=as_json,
        **extra,
    )
    try:
        dispatch_command(args, set())
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["properties"]["schema"]["const"] == results.SCHEMA_ID


def test_fingerprint_is_cheap_copy_stable_and_content_sensitive(tmp_path):
    small = tmp_path / "small.txt"
    small.write_bytes(b"ACGT" * 10)
    large = tmp_path / "large.bin"
    large.write_bytes(os.urandom(5 << 20))
    copy = tmp_path / "copy.bin"
    shutil.copy(large, copy)
    os.utime(copy, (1, 1))

    assert results.fingerprint(large) == results.fingerprint(copy)
    assert results.fingerprint(small) != results.fingerprint(large)
    with copy.open("r+b") as handle:  # a change in a sampled block is seen
        handle.seek(0)
        handle.write(b"X")
    assert results.fingerprint(large) != results.fingerprint(copy)
    assert results.fingerprint(tmp_path / "missing") is None
    assert VALIDATOR.evolve(schema=SCHEMA["$defs"]["fingerprint"]).is_valid(
        results.fingerprint(small)
    )


@pytest.mark.parametrize(
    ("output", "manifest"),
    [
        ("out/sample_snps.vcf.gz", "out/sample_snps.manifest.json"),
        ("out/sample_mtDNA.bam", "out/sample_mtDNA.manifest.json"),
        ("out/sample.tsv", "out/sample.manifest.json"),
    ],
)
def test_manifest_is_named_after_the_output(output, manifest):
    assert results.manifest_path_for(output) == Path(manifest)


def test_manifest_is_written_atomically_next_to_outputs(tmp_path):
    out_vcf = tmp_path / "s_snps.vcf.gz"

    def command(args):
        out_vcf.write_bytes(b"data")
        (tmp_path / "s_snps.vcf.gz.tbi").write_bytes(b"index")
        results.current_result().add_output(out_vcf, "snps", metrics={"variants": 3})
        results.current_result().add_metrics(variants=3)
        results.warn("low_coverage", "Low coverage.", depth=2.5, threshold=10)
        print("human text")

    assert dispatch(command, tmp_path, token="hunter2") == 0
    manifest = tmp_path / "s_snps.manifest.json"
    document = json.loads(manifest.read_text())
    assert_valid(document)
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []
    assert document["status"] == "ok" and document["manifest"] == str(manifest)
    (output,) = document["outputs"]
    assert output["role"] == "snps" and output["kind"] == "vcf"
    assert output["index"] == str(out_vcf) + ".tbi"
    assert output["metrics"] == {"variants": 3} and output["size_bytes"] == 4
    assert document["warnings"][0]["code"] == "low_coverage"
    assert document["provenance"]["params"]["token"] == "<redacted>"


@pytest.mark.parametrize(
    ("error", "code", "kind"),
    [
        (UsageError("--input required."), 2, "usage"),
        (MissingDependencyError("samtools missing"), 3, "missing_dependency"),
        (WGSExtractError("bad"), 1, "failure"),
        (RuntimeError("boom"), 1, "failure"),
    ],
)
def test_exit_codes(tmp_path, capsys, error, code, kind):
    def command(args):
        raise error

    assert dispatch(command, tmp_path, as_json=True) == code
    document = json.loads(capsys.readouterr().out)
    assert_valid(document)
    assert document["exit_code"] == code and document["error"]["kind"] == kind
    assert document["status"] == "failed"


def test_missing_tool_exits_3(tmp_path, capsys):
    from wgsextract_cli.core.dependency_checks import verify_dependencies

    def command(args):
        verify_dependencies(["wgse-no-such-tool"])

    assert dispatch(command, tmp_path, as_json=True) == 3
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == (
        "missing_dependency"
    )


def test_json_stdout_is_one_document_despite_child_output(tmp_path):
    # Run in a real process so fd-level output from children is exercised.
    script = textwrap.dedent(
        f"""
        import argparse, subprocess, sys
        from wgsextract_cli.cli.dispatch import dispatch_command
        from wgsextract_cli.core.results import current_result, human_output

        def command(args):
            print("python print")
            human_output("human text")
            subprocess.run([sys.executable, "-c", "print('child stdout')"])
            current_result().add_metrics(answer=42)

        dispatch_command(argparse.Namespace(
            func=command, command="test", _command="test", json=True,
            outdir={str(tmp_path)!r}), set())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    document = json.loads(proc.stdout)
    assert_valid(document)
    assert document["metrics"] == {"answer": 42}
    assert "python print" in proc.stderr and "child stdout" in proc.stderr
    assert "human text" not in proc.stdout + proc.stderr


def test_every_subcommand_gets_its_full_command_name():
    parser, _ = build_parser()
    args = parser.parse_args(["vcf", "snp", "--json"])
    assert args._command == "vcf snp" and args.json is True
    assert parser.parse_args(["info", "chip", "--list"])._command == "info chip"
    assert parser.parse_args(["info"])._command == "info"


def test_cli_usage_error_exits_2_with_a_manifest_on_stdout():
    rc, stdout, stderr = run_cli(["info", "--json"])
    assert rc == 2, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert document["command"] == "info" and document["error"]["kind"] == "usage"


def test_cli_extract_writes_a_valid_manifest(tmp_path):
    if not shutil.which("samtools"):
        pytest.skip("samtools not found in PATH")
    ensure_fake_data(str(FAKE_DIR))
    rc, stdout, stderr = run_cli(
        [
            "extract",
            "mt-bam",
            "--input",
            str(FAKE_DIR / "fake.bam"),
            "--ref",
            str(FAKE_DIR / "fake_ref.fa"),
            "--outdir",
            str(tmp_path),
            "--json",
        ]
    )
    assert rc == 0, stderr
    document = json.loads(stdout)
    assert_valid(document)
    assert document["command"] == "extract mt-bam"
    assert [o["role"] for o in document["outputs"]] == ["mt_bam"]
    assert document["inputs"][0]["fingerprint"].startswith("wgse-fp1:")
    assert document["provenance"]["reference"]["path"].endswith("fake_ref.fa")
    assert "samtools" in document["provenance"]["tools"]
    written = json.loads((tmp_path / "fake_mtDNA.manifest.json").read_text())
    assert written == document
