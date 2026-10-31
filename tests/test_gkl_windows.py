"""Native kernel gate; unlike optional tool tests, never skip missing GKL."""

import sys
from pathlib import Path

import pytest

from tests.htslib_helpers import run_wgsextract, vcf_calls
from tests.test_vcf_callers import genome as genome


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows kernel gate")
def test_windows_gatk_loads_native_utils_and_serial_avx_pairhmm(
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
    assert "Using the AVX-accelerated native PairHMM implementation" in log
    assert "Falling back to the MUCH slower LOGLESS_CACHING" not in log
    assert "Intel GKL Utils not loaded" not in log
    assert [call[:5] for call in vcf_calls(output / "gatk.vcf.gz")] == [genome.call]
    assert Path(str(output / "gatk.vcf.gz") + ".tbi").is_file()
