import argparse
from argparse import Namespace
from pathlib import Path

from wgsextract_cli.commands import benchmark


def test_benchmark_suite_defaults_to_heavy() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    benchmark.register(subparsers, argparse.ArgumentParser(add_help=False))

    args = parser.parse_args(["benchmark"])

    assert args.suite == "heavy"


def test_benchmark_result_names_include_cli_command_labels() -> None:
    assert (
        benchmark._name_with_command_label(
            "Reference N-base counting", ["ref", "count-ns", "--ref", "ref.fa"]
        )
        == "Reference N-base counting [ref count-ns]"
    )
    assert (
        benchmark._name_with_command_label(
            "Microarray CombinedKit generation", ["microarray", "--input", "sample.bam"]
        )
        == "Microarray CombinedKit generation [microarray]"
    )
    assert (
        benchmark._name_with_command_label(
            "BAM metadata and sequencing metrics",
            ["info", "--detailed", "--input", "sample.bam"],
            "info --detailed",
        )
        == "BAM metadata and sequencing metrics [info --detailed]"
    )


def test_cpu_frequency_handles_psutil_exception(monkeypatch) -> None:
    def raise_cpu_freq_error():
        raise RuntimeError("cpu frequency unavailable")

    monkeypatch.setattr(benchmark.psutil, "cpu_freq", raise_cpu_freq_error)

    assert benchmark._cpu_frequency() is None


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
        command_label: str | None = None,
    ) -> benchmark.BenchmarkResult:
        del args, command_args, logs_dir, command_label
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
        "_run_heavy_processing_steps",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("core suite should not run heavy benchmark steps")
        ),
    )
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
    monkeypatch.setattr(
        benchmark,
        "default_thread_tuning_profile",
        lambda: Namespace(threads=8, label="8", reason="all available cores"),
    )

    benchmark.run(
        Namespace(
            profile="smoke",
            coverage=None,
            full_size=False,
            build="hg38",
            seed=1,
            target_count=None,
            suite="core",
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
    assert "Thread policy:" in stdout
    assert "Policy: all available cores" in stdout
    assert "Benchmark base file:" in stdout
    assert "Fake BAM generator: fast streaming reference-backed SNP generator" in stdout
    assert "1.5 KiB (1,536 bytes)" in stdout
    assert stdout.count("Generate deterministic BAM foundation") == 1
    assert "PASS" in stdout
    assert "1.25" in stdout
    assert "WGSExtract CLI Benchmark Summary" in stdout
    assert "Suite: core" in stdout
    assert "Thread policy: all available cores" in stdout
    assert "Machine: TestOS 1.0 | Test CPU" in stdout
    assert "External tools: samtools" in stdout


def test_heavy_suite_runs_extra_processing_steps(
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
        command_label: str | None = None,
    ) -> benchmark.BenchmarkResult:
        del args, command_args, logs_dir, command_label
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in expected_outputs:
            touch(output)
        return benchmark.BenchmarkResult(
            name=name,
            slug=slug,
            status="PASS",
            seconds=0.1,
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
            seconds=0.1,
            command=["internal", slug],
            output_dir=str(output_dir),
            expected_outputs=[str(output) for output in expected_outputs],
        )

    called: dict[str, Path | str | None] = {}

    def fake_heavy_steps(**kwargs) -> None:
        called["analysis_bam"] = kwargs["analysis_bam"]
        called["snp_vcf"] = kwargs["snp_vcf"]
        called["region"] = kwargs["region"]
        kwargs["record"](
            benchmark.BenchmarkResult(
                name="Heavy sentinel benchmark",
                slug="heavy-sentinel",
                status="PASS",
                seconds=0.2,
                command=["fake", "heavy"],
                output_dir=str(kwargs["steps_dir"] / "heavy-sentinel"),
            )
        )

    monkeypatch.setattr(benchmark, "_run_cli_step", fake_cli_step)
    monkeypatch.setattr(benchmark, "_run_internal_step", fake_internal_step)
    monkeypatch.setattr(benchmark, "_run_heavy_processing_steps", fake_heavy_steps)
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
            "cpu_frequency": None,
            "ram_total": "16.0 GiB (17,179,869,184 bytes)",
            "ram_available": "8.0 GiB (8,589,934,592 bytes)",
            "ram_speed": None,
            "benchmark_filesystem": str(run_dir),
            "disk_total": "100.0 GiB (107,374,182,400 bytes)",
            "disk_free": "50.0 GiB (53,687,091,200 bytes)",
            "drive_model": None,
            "drive_speed": None,
        },
    )
    monkeypatch.setattr(
        benchmark,
        "default_thread_tuning_profile",
        lambda: Namespace(threads=8, label="8", reason="all available cores"),
    )
    monkeypatch.setattr(benchmark, "_benchmark_external_tools", lambda: [])

    benchmark.run(
        Namespace(
            profile="smoke",
            coverage=None,
            full_size=False,
            build="hg38",
            seed=1,
            target_count=None,
            suite="heavy",
            region="chrM",
            keep_going=False,
            outdir=str(tmp_path),
            _explicit_dests={"outdir"},
            debug=False,
            quiet=False,
            threads=1,
            memory=None,
        )
    )

    stdout = capsys.readouterr().out
    assert "Suite: heavy" in stdout
    assert "Heavy sentinel benchmark" in stdout
    assert isinstance(called["analysis_bam"], Path)
    assert called["analysis_bam"].name == "benchmark_R1_aligned.bam"
    assert isinstance(called["snp_vcf"], Path)
    assert called["snp_vcf"].name == "snps.vcf.gz"
    assert called["region"] == "chrM"


def test_heavy_processing_steps_include_local_only_commands(
    tmp_path: Path, monkeypatch
) -> None:
    def touch(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    cli_slugs: list[str] = []
    pipe_slugs: list[str] = []
    internal_slugs: list[str] = []
    records: list[benchmark.BenchmarkResult] = []

    def fake_cli_step(
        args: Namespace,
        name: str,
        slug: str,
        command_args: list[str],
        output_dir: Path,
        logs_dir: Path,
        expected_outputs: list[Path],
        command_label: str | None = None,
    ) -> benchmark.BenchmarkResult:
        del args, command_args, logs_dir, command_label
        cli_slugs.append(slug)
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in expected_outputs:
            touch(output)
        return benchmark.BenchmarkResult(
            name=name,
            slug=slug,
            status="PASS",
            seconds=0.1,
            command=["fake", slug],
            output_dir=str(output_dir),
            expected_outputs=[str(output) for output in expected_outputs],
        )

    def fake_pipe_step(
        args: Namespace,
        name: str,
        slug: str,
        command_args: list[str],
        input_file: Path,
        output_file: Path,
        output_dir: Path,
        logs_dir: Path,
        expected_outputs: list[Path],
        command_label: str | None = None,
    ) -> benchmark.BenchmarkResult:
        del args, command_args, input_file, output_file, logs_dir, command_label
        pipe_slugs.append(slug)
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in expected_outputs:
            touch(output)
        return benchmark.BenchmarkResult(
            name=name,
            slug=slug,
            status="PASS",
            seconds=0.1,
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
        internal_slugs.append(slug)
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in expected_outputs:
            touch(output)
        return benchmark.BenchmarkResult(
            name=name,
            slug=slug,
            status="PASS",
            seconds=0.1,
            command=["internal", slug],
            output_dir=str(output_dir),
            expected_outputs=[str(output) for output in expected_outputs],
        )

    monkeypatch.setattr(benchmark, "_run_cli_step", fake_cli_step)
    monkeypatch.setattr(benchmark, "_run_cli_pipe_step", fake_pipe_step)
    monkeypatch.setattr(benchmark, "_run_internal_step", fake_internal_step)
    monkeypatch.setattr(benchmark, "_benchmark_tool_available", lambda tool: False)

    def record(result: benchmark.BenchmarkResult) -> benchmark.BenchmarkResult:
        records.append(result)
        return result

    args = Namespace(threads=1, memory=None, debug=False, quiet=False)
    benchmark._run_heavy_processing_steps(
        args=args,
        record=record,
        analysis_bam=tmp_path / "sample.bam",
        generated_bam=tmp_path / "fake.bam",
        ref_path=tmp_path / "ref.fa",
        target_tab_gz=tmp_path / "targets.tab.gz",
        snp_vcf=tmp_path / "snps.vcf.gz",
        unalign_r1=tmp_path / "sample_R1.fastq.gz",
        unalign_r2=tmp_path / "sample_R2.fastq.gz",
        steps_dir=tmp_path / "steps",
        logs_dir=tmp_path / "logs",
        build="hg38",
        region="chrM",
    )

    assert "12a-ref-count-ns" in cli_slugs
    assert "12b-ref-verify" in cli_slugs
    assert "17b-analyze-batch-gen" in cli_slugs
    assert "18b-repair-ftdna-bam" in pipe_slugs
    assert "18c-repair-ftdna-vcf" in pipe_slugs
    assert "17a-analyze-batch-fixture" in internal_slugs
    assert "18a-repair-fixtures" in internal_slugs


def test_200mb_profile_targets_scaled_fake_bam() -> None:
    profile = benchmark.PROFILE_DEFAULTS["200mb"]

    assert profile["coverage"] == 71.0
    assert profile["full_size"] is False
    assert profile["region"] is None


def test_macos_benchmark_threads_use_performance_cores(monkeypatch) -> None:
    args = Namespace(threads=None)
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(benchmark.psutil, "cpu_count", lambda logical=False: 10)
    monkeypatch.setattr(
        benchmark,
        "default_thread_tuning_profile",
        lambda: Namespace(
            threads=8,
            label="8",
            reason="Apple Silicon performance-core count",
        ),
    )

    plan = benchmark._benchmark_thread_plan(args)

    assert plan.label == "8"
    assert plan.default_threads == 8
    assert plan.per_step_threads == {}
    args._benchmark_thread_plan = plan
    assert benchmark._benchmark_threads_for_step(args, "04-fastq-align") == 8


def test_benchmark_keeps_explicit_thread_override(monkeypatch) -> None:
    args = Namespace(threads=4)
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(benchmark, "_command_output", lambda command: "8")

    plan = benchmark._benchmark_thread_plan(args)

    assert plan.label == "4"
    assert plan.default_threads == 4
    assert plan.per_step_threads == {}


def test_cli_command_uses_step_thread_selection() -> None:
    args = Namespace(
        debug=False,
        quiet=False,
        memory=None,
        _benchmark_thread_plan=benchmark.BenchmarkThreadPlan(
            "mixed", 8, {"04-fastq-align": 10}, "test mixed policy"
        ),
    )

    align_threads = benchmark._benchmark_threads_for_step(args, "04-fastq-align")
    sort_threads = benchmark._benchmark_threads_for_step(args, "05-bam-sort")

    assert align_threads == 10
    assert sort_threads == 8
    assert benchmark._cli_command(args, ["align"], align_threads)[-2:] == [
        "--threads",
        "10",
    ]
