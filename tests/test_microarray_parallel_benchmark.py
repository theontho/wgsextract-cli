import sys
from pathlib import Path

import pytest

from scripts.benchmark_microarray_parallel import export_hashes, measure


@pytest.mark.skipif(sys.platform != "linux", reason="Linux wait4 resource accounting")
def test_cpu_accounting_includes_reaped_descendants(tmp_path: Path) -> None:
    child = (
        "import time; end=time.process_time()+0.3\nwhile time.process_time()<end: pass"
    )
    command = [
        sys.executable,
        "-c",
        f"import subprocess,sys; subprocess.run([sys.executable,'-c',{child!r}],check=True)",
    ]
    result = measure(command, tmp_path / "measurement")
    assert result["cpu_seconds"] >= 0.25
    assert result["wall_seconds"] >= 0.25
    assert result["peak_process_rss_bytes"] > 0
    assert result["sampled_peak_tree_rss_bytes"] > 0


def test_export_comparison_ignores_variable_intermediates(tmp_path: Path) -> None:
    (tmp_path / "kit_CombinedKit.txt").write_text("stable\n", newline="\n")
    (tmp_path / "kit.vcf.gz").write_bytes(b"variable headers")
    (tmp_path / "stderr.log").write_text("timing\n", newline="\n")
    hashes = export_hashes(tmp_path)
    assert set(hashes) == {"kit_CombinedKit.txt"}
    (tmp_path / "kit_CombinedKit.txt").write_text("changed\n", newline="\n")
    assert export_hashes(tmp_path) != hashes


@pytest.mark.skipif(sys.platform != "linux", reason="Linux wait4 resource accounting")
def test_failed_measurement_does_not_report_success(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="failed"):
        measure([sys.executable, "-c", "raise SystemExit(7)"], tmp_path / "failed")
