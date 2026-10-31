import sys
from pathlib import Path

import pytest

from scripts.benchmark_microarray_parallel import export_hashes, main, measure


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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux benchmark harness")
def test_reference_symlink_keeps_its_library_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "elsewhere" / "genome.fa"
    actual.parent.mkdir()
    actual.write_text(">chr20\nA\n", newline="\n")
    alias = tmp_path / "library" / "genomes" / "genome.fa"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(actual)
    bam, config, targets = (
        tmp_path / "sample.bam",
        tmp_path / "config.toml",
        tmp_path / "targets.tab",
    )
    for path in (bam, config, targets):
        path.write_bytes(b"fixture")

    def check_command(command: list[str], output: Path) -> dict[str, float]:
        assert Path(command[command.index("--ref") + 1]) == alias
        raise RuntimeError("reference path checked")

    monkeypatch.setattr("scripts.benchmark_microarray_parallel.measure", check_command)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark_microarray_parallel.py",
            "--input",
            str(bam),
            "--ref",
            str(alias),
            "--config",
            str(config),
            "--ref-vcf-tab",
            str(targets),
            "--outdir",
            str(tmp_path / "output"),
        ],
    )
    with pytest.raises(RuntimeError, match="reference path checked"):
        main()
