"""Scatter budgets, ordered gathering, and failure isolation without Java."""

from argparse import Namespace
from pathlib import Path
from threading import Barrier, Event

import pytest

from wgsextract_cli.commands.vcf import callers, gatk_scatter
from wgsextract_cli.core import paths, resource_estimates
from wgsextract_cli.core.utils import WGSExtractError


def _reference(tmp_path):
    ref = tmp_path / "ref.fa"
    Path(str(ref) + ".fai").write_text("chr2\t10\t0\t10\t11\nchr1\t20\t0\t20\t21\n")
    return str(ref)


def test_scatter_merges_requests_and_gathers_in_reference_order(tmp_path, monkeypatch):
    monkeypatch.setattr(gatk_scatter, "available_memory", lambda: 24 * 1024**3)
    shards = gatk_scatter.scatter_intervals(
        _reference(tmp_path), "chr1:4-15,chr2:2-8,chr1:10-20", "3"
    )
    assert shards == [
        [("chr2", 2, 8), ("chr1", 4, 4)],
        [("chr1", 5, 12)],
        [("chr1", 13, 20)],
    ]


@pytest.mark.parametrize(
    ("ram", "threads", "workers"), [(12, "4", 2), (64, "2", 2), (1, "4", 1)]
)
def test_scatter_respects_memory_and_cpu_budgets(
    tmp_path, monkeypatch, ram, threads, workers
):
    monkeypatch.setattr(gatk_scatter, "available_memory", lambda: ram * 1024**3)
    shards = gatk_scatter.scatter_intervals(_reference(tmp_path), None, threads)
    assert len(shards) == workers
    flattened = [
        (contig, position)
        for shard in shards
        for contig, start, end in shard
        for position in range(start, end + 1)
    ]
    assert flattened == [
        *(("chr2", position) for position in range(1, 11)),
        *(("chr1", position) for position in range(1, 21)),
    ]


@pytest.mark.parametrize("region", ["unknown", "chr1:0-5", "chr1:9-8", "chr1:1-21"])
def test_scatter_rejects_bad_regions_before_starting_workers(tmp_path, region):
    with pytest.raises(WGSExtractError, match="Invalid GATK scatter region"):
        gatk_scatter.scatter_intervals(_reference(tmp_path), region, "2")


def test_unknown_memory_does_not_start_parallel_jvms(tmp_path, monkeypatch):
    def unavailable():
        raise OSError("no memory measurement")

    monkeypatch.setattr(gatk_scatter, "available_memory", unavailable)
    assert len(gatk_scatter.scatter_intervals(_reference(tmp_path), None, "8")) == 1


@pytest.mark.parametrize("custom_temp", [False, True])
def test_scatter_windows_argv_and_gather_order(
    tmp_path, monkeypatch, caplog, custom_temp
):
    monkeypatch.setattr(paths, "sys", Namespace(platform="win32"))
    completed_second = Event()
    commands = []
    work = tmp_path / "parts"
    work.mkdir()
    temporary = tmp_path / "java"
    temporary.mkdir()
    output = tmp_path / "gatk.vcf.gz"
    monkeypatch.setattr(
        gatk_scatter,
        "configured_temp_directory",
        lambda: str(work) if custom_temp else None,
    )
    replace = Path.replace

    def cross_volume_replace(source, target):
        if custom_temp and source.parent == work:
            import errno

            raise OSError(errno.EXDEV, "different filesystems")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", cross_volume_replace)

    def worker(command, _log, _cancelled):
        commands.append(command)
        name = command[command.index("-O") + 1]
        part = Path(name.replace("\\", "/"))
        if part.name.startswith("000000"):
            assert completed_second.wait(5)
        else:
            completed_second.set()
        part.write_text(part.stem)

    def gather(command):
        listed = Path(command[command.index("-f") + 1]).read_text().splitlines()
        Path(command[command.index("-o") + 1]).write_text(
            ",".join(Path(part).read_text() for part in listed)
        )

    monkeypatch.setattr(gatk_scatter, "_run_worker", worker)
    monkeypatch.setattr(gatk_scatter, "run_command", gather)
    monkeypatch.setattr(
        gatk_scatter,
        "ensure_vcf_indexed",
        lambda path: Path(path + ".tbi").write_text("index"),
    )
    gatk_scatter.call_scattered(
        [
            "gatk",
            "HaplotypeCaller",
            "-R",
            r"R:\Reference data\ref.fa",
            "-I",
            r"R:\Input data\sample.bam",
            "--flow-mode",
            "ADVANCED",
        ],
        [[("chr2", 1, 10)], [("chr1", 1, 20)]],
        str(output),
        work,
        str(temporary),
    )
    assert output.read_text() == "000000.vcf,000001.vcf"
    assert Path(str(output) + ".tbi").read_text() == "index"
    assert len(commands) == 2
    assert not list(tmp_path.glob(".wgse_gather_*"))
    assert "can change annotations and genotypes" in caplog.text
    assert "away from interval boundaries" in caplog.text
    for command in commands:
        assert command[command.index("--native-pair-hmm-threads") + 1] == "1"
        assert "-Xmx4g" in command[command.index("--java-options") + 1]
        for flag in ("-R", "-I", "-O", "-L", "--tmp-dir"):
            assert "/" not in command[command.index(flag) + 1]
        request = Path(command[command.index("-L") + 1].replace("\\", "/"))
        assert request.read_text() in ("chr2:1-10\n", "chr1:1-20\n")


def test_worker_failure_cancels_peers_and_preserves_existing_output(
    tmp_path, monkeypatch
):
    barrier = Barrier(2)
    cancelled_peer = Event()
    output = tmp_path / "gatk.vcf.gz"
    output.write_text("existing output")
    Path(str(output) + ".tbi").write_text("existing index")
    work = tmp_path / "parts"
    work.mkdir()
    temporary = tmp_path / "java"
    temporary.mkdir()

    def worker(_command, log, cancelled):
        barrier.wait(timeout=5)
        if log.name.startswith("000000"):
            raise WGSExtractError("worker failed")
        assert cancelled.wait(5)
        cancelled_peer.set()

    def no_gather(_command):
        raise AssertionError("gather ran after a worker failed")

    monkeypatch.setattr(gatk_scatter, "_run_worker", worker)
    monkeypatch.setattr(gatk_scatter, "run_command", no_gather)
    with pytest.raises(WGSExtractError, match="worker failed"):
        gatk_scatter.call_scattered(
            ["gatk", "HaplotypeCaller"],
            [[("chr1", 1, 10)], [("chr1", 11, 20)]],
            str(output),
            work,
            str(temporary),
        )
    assert cancelled_peer.is_set()
    assert output.read_text() == "existing output"
    assert Path(str(output) + ".tbi").read_text() == "existing index"


def test_cancellation_reaps_java_children_and_launcher(monkeypatch):
    events = []

    class Process:
        pid = 123

        def terminate(self):
            events.append("terminate launcher")

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append("wait launcher")
            return 0

    class Java:
        def terminate(self):
            events.append("terminate Java")

        def kill(self):
            events.append("kill Java")

    java = Java()
    monkeypatch.setattr(
        gatk_scatter.psutil,
        "Process",
        lambda _pid: Namespace(children=lambda **_: [java]),
    )
    monkeypatch.setattr(
        gatk_scatter.psutil, "wait_procs", lambda *_a, **_kw: ([], [java])
    )
    gatk_scatter._stop_worker(Process())
    assert events == [
        "terminate Java",
        "kill Java",
        "terminate launcher",
        "wait launcher",
    ]


def test_gatk_scatter_rejects_gpu_before_any_work():
    with pytest.raises(WGSExtractError, match="cannot be combined"):
        callers.cmd_gatk(Namespace(scatter=True, gpu=True))


def test_scatter_preflight_counts_all_worker_heaps(tmp_path):
    estimate = resource_estimates.estimate_resources(
        "gatk", input_paths=[], output_dir=str(tmp_path), threads="2", gatk_workers=2
    )
    assert estimate.resources[0].components == {"caller": 12 * 1024**3}


def test_scatter_preflight_charges_staging_to_output_not_java_volume(monkeypatch):
    mib = 1024**2
    monkeypatch.setattr(resource_estimates, "_input_size", lambda _: 100 * mib)
    monkeypatch.setattr(
        resource_estimates,
        "_disk_space",
        lambda path: (
            ("java", 100 * 1024**3) if path == "java-temp" else ("output", 15 * mib)
        ),
    )
    estimate = resource_estimates.estimate_resources(
        "gatk",
        input_paths=["sample.bam"],
        output_dir="results",
        temp_dir="java-temp",
        threads="2",
        gatk_workers=2,
    )
    output_disk, java_disk = estimate.resources[1:]
    assert output_disk.status == "LOW"
    assert output_disk.components["scatter_parts"] == 10 * mib
    assert output_disk.components["scatter_indexes"] > 0
    assert output_disk.paths == ("results",)
    assert java_disk.components == {"temporary": 20 * mib}
    assert java_disk.status == "OK"
