"""CLI usage errors and invalid inputs must exit cleanly: no traceback, a message,
and a non-zero exit code. Each case runs as a real subprocess."""

import subprocess
from pathlib import Path

import pytest

from tests.smoke_utils import run_cli

# Generous ceiling: the point is "exits instead of hanging", and a cold
# interpreter start on Windows CI can take several seconds.
TIMEOUT = 60

# Subcommands invoked without their required inputs.
BARE_COMMANDS = [
    ["info"],
    ["info", "--detailed"],
    ["info", "calculate-coverage"],
    ["info", "coverage-sample"],
    ["bam", "sort"],
    ["bam", "index"],
    ["bam", "unindex"],
    ["bam", "unsort"],
    ["bam", "to-cram"],
    ["bam", "to-bam"],
    ["bam", "unalign"],
    ["bam", "identify"],
    ["extract", "bam-subset"],
    ["extract", "mito"],
    ["extract", "ydna"],
    ["extract", "unmapped"],
    ["vcf", "snp"],
    ["vcf", "indel"],
    ["vcf", "annotate"],
    ["vcf", "filter"],
    ["qc", "vcf"],
    ["qc", "fastp"],
    ["qc", "fastqc"],
    ["qc", "coverage-wgs"],
    ["qc", "coverage-wes"],
    ["microarray"],
    ["lineage", "mt-haplogroup"],
    ["lineage", "y-haplogroup"],
    ["pet-align"],
    ["ref", "download"],
    ["ref", "index"],
    ["align"],
]

# Commands given a directory where a file is expected ("{dir}").
DIRECTORY_INPUTS = [
    ["--input", "{dir}", "info"],
    ["--input", "{dir}", "info", "--detailed"],
    ["--input", "{dir}", "info", "calculate-coverage"],
    ["--input", "{dir}", "info", "coverage-sample"],
    ["--input", "{dir}", "bam", "sort"],
    ["--input", "{dir}", "bam", "index"],
    ["--input", "{dir}", "bam", "unindex"],
    ["--input", "{dir}", "bam", "unsort"],
    ["--input", "{dir}", "bam", "to-cram"],
    ["--input", "{dir}", "bam", "to-bam"],
    ["--input", "{dir}", "bam", "unalign", "--r1", "r1", "--r2", "r2"],
    ["--input", "{dir}", "bam", "identify"],
    ["--input", "{dir}", "extract", "bam-subset", "-f", "0.1"],
    ["--input", "{dir}", "extract", "mito"],
    ["--input", "{dir}", "extract", "ydna"],
    ["--input", "{dir}", "extract", "unmapped", "--r1", "u1", "--r2", "u2"],
    ["--input", "{dir}", "vcf", "snp"],
    ["--input", "{dir}", "vcf", "indel"],
    ["--input", "{dir}", "vcf", "annotate", "--ann-vcf", "{dir}", "--cols", "ID"],
    ["--input", "{dir}", "vcf", "filter", "--expr", "QUAL>30"],
    ["--input", "{dir}", "qc", "vcf"],
    ["--input", "{dir}", "qc", "coverage-wgs"],
    ["--input", "{dir}", "qc", "coverage-wes", "--bed", "{dir}"],
    ["--input", "{dir}", "microarray"],
    ["--input", "{dir}", "lineage", "mt-haplogroup"],
    ["--input", "{dir}", "lineage", "y-haplogroup"],
    ["--input", "{dir}", "repair", "ftdna-bam"],
    ["--input", "{dir}", "repair", "ftdna-vcf"],
    ["qc", "fastp", "--r1", "{dir}"],
    ["qc", "fastqc", "--fastq", "{dir}"],
    ["pet-align", "--r1", "{dir}", "--species", "dog", "--ref", "{dir}"],
    ["ref", "download", "--url", "http://fake", "--out", "{dir}"],
    ["--ref", "{dir}", "ref", "index"],
    ["align", "--r1", "{dir}"],
]


def _assert_clean_failure(args: list[str]) -> str:
    try:
        rc, stdout, stderr = run_cli(args, stdin_str="", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.fail(f"wgsextract {' '.join(args)} did not exit within {TIMEOUT}s")
    output = stdout + stderr
    assert "Traceback" not in output, output
    assert output.strip(), f"wgsextract {' '.join(args)} printed nothing"
    assert rc != 0, f"wgsextract {' '.join(args)} exited 0:\n{output}"
    return stderr


def test_help_exits_zero() -> None:
    rc, stdout, stderr = run_cli(["--help"], timeout=TIMEOUT)
    assert rc == 0
    assert "Traceback" not in stdout + stderr
    assert "usage" in stdout.lower()


@pytest.mark.parametrize("command", ["ftdna-bam", "ftdna-vcf"])
def test_repair_accepts_empty_stdin(command: str) -> None:
    rc, stdout, stderr = run_cli(["repair", command], stdin_str="", timeout=TIMEOUT)
    assert "Traceback" not in stdout + stderr
    assert rc == 0, stderr


@pytest.mark.parametrize("args", BARE_COMMANDS, ids=" ".join)
def test_missing_inputs_fail_cleanly(args: list[str]) -> None:
    _assert_clean_failure(args)


@pytest.mark.parametrize("args", DIRECTORY_INPUTS, ids=" ".join)
def test_directory_inputs_fail_cleanly(args: list[str], tmp_path: Path) -> None:
    directory = tmp_path / "is_a_directory"
    directory.mkdir()
    _assert_clean_failure([a.replace("{dir}", str(directory)) for a in args])


@pytest.mark.parametrize(
    "args",
    [
        ["align", "--ref", "{dir}"],
        ["qc", "fastp"],
        ["pet-align", "--species", "dog", "--ref", "{dir}"],
    ],
    ids=" ".join,
)
def test_missing_r1_is_reported(args: list[str], tmp_path: Path) -> None:
    stderr = _assert_clean_failure([a.replace("{dir}", str(tmp_path)) for a in args])
    assert "--r1 is required" in stderr
