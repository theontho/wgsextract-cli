from argparse import Namespace
from pathlib import Path

from wgsextract_cli.commands import benchmark


def test_benchmark_prints_progress_lines_and_base_file_size(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    def touch(path: Path, size: int = 1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def fake_cli_step(
        args: Namespace,
        name: str,
        slug: str,
        command_args: list[str],
        output_dir: Path,
        logs_dir: Path,
        expected_outputs: list[Path],
    ) -> benchmark.BenchmarkResult:
        del args, command_args, logs_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in expected_outputs:
            touch(output, size=1536 if output.name == "fake.bam" else 1)
        return benchmark.BenchmarkResult(
            name=name,
            slug=slug,
            status="PASS",
            seconds=1.25,
            command=["fake", slug],
            output_dir=str(output_dir),
            expected_outputs=[str(output) for output in expected_outputs],
        )

    def fake_internal_step(
        name: str,
        slug: str,
        output_dir: Path,
        func,
        expected_outputs: list[Path],
    ) -> benchmark.BenchmarkResult:
        del func
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in expected_outputs:
            touch(output)
        return benchmark.BenchmarkResult(
            name=name,
            slug=slug,
            status="PASS",
            seconds=0.5,
            command=["internal", slug],
            output_dir=str(output_dir),
            expected_outputs=[str(output) for output in expected_outputs],
        )

    monkeypatch.setattr(benchmark, "_run_cli_step", fake_cli_step)
    monkeypatch.setattr(benchmark, "_run_internal_step", fake_internal_step)
    monkeypatch.setattr(
        benchmark,
        "_machine_stats",
        lambda run_dir: {
            "os": "TestOS 1.0",
            "python": "Python 3.10",
            "architecture": "test64",
            "cpu_model": "Test CPU",
            "physical_cores": 4,
            "logical_cores": 8,
            "cpu_frequency": "3.20 GHz",
            "ram_total": "16.0 GiB (17,179,869,184 bytes)",
            "ram_available": "8.0 GiB (8,589,934,592 bytes)",
            "ram_speed": "3200 MHz",
            "benchmark_filesystem": str(run_dir),
            "disk_total": "100.0 GiB (107,374,182,400 bytes)",
            "disk_free": "50.0 GiB (53,687,091,200 bytes)",
            "drive_model": "Test Drive",
            "drive_speed": "NVMe",
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_benchmark_external_tools",
        lambda: [
            {
                "name": "samtools",
                "required": True,
                "active": True,
                "status": "active",
                "purpose": "BAM/CRAM/FASTA indexing and conversion",
                "path": "/usr/bin/samtools",
                "runtime": "native",
                "version": "samtools 1.22",
            }
        ],
    )

    benchmark.run(
        Namespace(
            profile="smoke",
            coverage=None,
            full_size=False,
            build="hg38",
            seed=1,
            target_count=None,
            region=None,
            keep_going=False,
            outdir=str(tmp_path),
            _explicit_dests={"outdir"},
            debug=False,
            quiet=False,
            threads=None,
            memory=None,
        )
    )

    stdout = capsys.readouterr().out
    assert "Machine stats:" in stdout
    assert "OS: TestOS 1.0" in stdout
    assert "CPU: Test CPU" in stdout
    assert "Cores: 4 physical / 8 logical" in stdout
    assert "RAM: 16.0 GiB" in stdout
    assert "Drive: Test Drive" in stdout
    assert "External tools used or checked by this benchmark:" in stdout
    assert "samtools [required, active]" in stdout
    assert "native: /usr/bin/samtools" in stdout
    assert "WGSExtract CLI Benchmark Progress" in stdout
    assert "Benchmark base file:" in stdout
    assert "Fake BAM generator: fast streaming reference-backed SNP generator" in stdout
    assert "1.5 KiB (1,536 bytes)" in stdout
    assert stdout.count("Generate deterministic BAM foundation") == 1
    assert "PASS" in stdout
    assert "1.25" in stdout
    assert "WGSExtract CLI Benchmark Summary" in stdout
    assert "Machine: TestOS 1.0 | Test CPU" in stdout
    assert "External tools: samtools" in stdout
