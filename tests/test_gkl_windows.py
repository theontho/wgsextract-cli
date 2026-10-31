"""Native kernel gate; unlike optional tool tests, never skip missing GKL."""

import csv
import runpy
import sys
from pathlib import Path

import pytest

from tests.htslib_helpers import run_wgsextract, vcf_calls
from tests.test_vcf_callers import genome as genome
from wgsextract_cli.core.utils import run_command


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows kernel gate")
def test_windows_gatk_loads_native_utils_omp_pairhmm_and_smithwaterman(
    genome, tmp_path, capfd
):
    output = tmp_path / "out"
    run_wgsextract(
        "vcf", "gatk", "--input", genome.bam, "--ref", genome.ref,
        "--outdir", str(output), "-r", "chr1:900-1100",
    )  # fmt: skip
    captured = capfd.readouterr()
    log = captured.out + captured.err
    (tmp_path / "gatk-native.log").write_text(log)
    assert "Intel GKL Utils loaded" in log
    assert (
        "Using the OpenMP multi-threaded AVX-accelerated native PairHMM implementation"
        in log
    )
    assert "Using AVX accelerated SmithWaterman implementation" in log
    assert "Ignoring request for" not in log
    assert "Falling back to the MUCH slower LOGLESS_CACHING" not in log
    assert "Intel GKL Utils not loaded" not in log
    assert [call[:5] for call in vcf_calls(output / "gatk.vcf.gz")] == [genome.call]
    assert Path(str(output / "gatk.vcf.gz") + ".tbi").is_file()
    body = run_command(
        ["bcftools", "view", "-H", str(output / "gatk.vcf.gz")], capture_output=True
    ).stdout
    (tmp_path / "decoded-body.vcf").write_bytes(body.encode("utf-8"))
    golden = Path(__file__).parent / "fixtures/gatk-linux-avx2/body.tsv"
    assert body == golden.read_text(), "Native GATK VCF fields differ from Linux AVX2"


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launcher")
def test_windows_gatk_accepts_launcher_java_options():
    result = run_command(
        ["gatk", "--java-options", "-Xmx512m -XX:ActiveProcessorCount=1", "--version"],
        capture_output=True,
    )
    assert "v4.6.2.0" in result.stdout + result.stderr


@pytest.mark.parametrize("regression", ["likelihood", "sw", "missing"])
def test_native_probe_comparison_rejects_regressions(tmp_path, regression):
    directory = (
        Path(__file__).resolve().parents[1] / "packaging/windows/recipes/gkl/tests"
    )
    probe = runpy.run_path(str(directory / "compare_jni.py"))
    baseline = directory / "linux-avx2-golden.tsv"
    records = probe["rows"](baseline)
    if regression == "missing":
        records.pop()
    elif regression == "sw":
        next(row for row in records if row["kind"] == "sw")["value"] = "1X"
    else:
        row = next(row for row in records if row["kind"] == "pair")
        row["value"] = (float.fromhex(row["value"]) + 0.01).hex()
    candidate = tmp_path / "candidate.tsv"
    with candidate.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(records)
    assert not probe["compare"](candidate, baseline)["pass"]
