"""`vcf gatk --scatter`: budgets, ordered gathering and failure isolation, with
GATK faked at its process boundary."""

import errno
import json
import os
import platform
import random
import shutil
import time
from argparse import Namespace
from pathlib import Path

import psutil
import pytest

from tests.disk_volumes import GIB, Volumes
from tests.fake_tools import FakeGatk, require_bare_name_fakes
from tests.htslib_helpers import (
    require_runnable,
    run_wgsextract,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli.commands.vcf import gatk_scatter
from wgsextract_cli.core import paths, resource_estimates
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.utils import UsageError, WGSExtractError, run_command
from wgsextract_cli.main import main


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


@pytest.fixture(scope="module")
def ultima(tmp_path_factory) -> Namespace:
    """Flow-aware Ultima reads (FO, tp, t0) over chr2 and chr1, in that
    reference order, each with a homozygous SNP at 1,000; and the same reads
    as an Illumina BAM."""
    require_runnable("samtools")
    work = tmp_path_factory.mktemp("ultima")
    rng = random.Random(3)
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(3000)) for c in ("chr2", "chr1")
    }
    ref = write_fasta(work / "ref.fa", seqs)
    run_command(["samtools", "faidx", str(ref)])
    run_command(["samtools", "dict", "-o", str(work / "ref.dict"), str(ref)])
    calls = []
    for read_group in ("ULTIMA\tFO:TGCA", "ILLUMINA"):
        lines = ["@HD\tVN:1.6\tSO:unsorted"]
        lines += [f"@SQ\tSN:{c}\tLN:3000" for c in seqs]
        lines.append(f"@RG\tID:rg\tSM:S\tPL:{read_group}")
        for contig, seq in seqs.items():
            alt = {"A": "C", "C": "G", "G": "T", "T": "A"}[seq[999]]
            sample = seq[:999] + alt + seq[1000:]
            calls.append((contig, "1000", seq[999], alt, "1/1"))
            for i, start in enumerate(range(800, 1100, 5)):
                lines.append(
                    f"{contig}{i}\t{16 if i % 2 else 0}\t{contig}\t{start}\t60\t100M"
                    f"\t*\t0\t0\t{sample[start - 1 : start + 99]}\t{'I' * 100}"
                    f"\ttp:B:c{',0' * 100}\tt0:Z:{'I' * 100}\tRG:Z:rg"
                )
        name = "ultima" if read_group != "ILLUMINA" else "illumina"
        sam_to_bam(lines, work / f"{name}.bam")
    return Namespace(
        ref=str(ref),
        bam=str(work / "ultima.bam"),
        illumina=str(work / "illumina.bam"),
        calls=calls[:2],
    )


@pytest.fixture
def gatk(tmp_path, monkeypatch) -> FakeGatk:
    """GATK at its process boundary (see FakeGatk), with RAM for 4 workers."""
    require_bare_name_fakes()
    require_runnable("samtools", "bcftools", "tabix")
    bcftools = shutil.which("bcftools") or pytest.skip("bcftools is not on PATH")
    bindir = tmp_path / "bin"
    fake = FakeGatk(bindir, bcftools)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    # The fake runs anywhere; real GATK is refused on Linux ARM64 (#236).
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    clear_tool_caches()
    monkeypatch.setattr(gatk_scatter, "available_memory", lambda: 24 * 1024**3)
    return fake


def _scatter(
    capsys, ultima: Namespace, tmp_path: Path, *extra: str
) -> tuple[dict, str]:
    """`vcf gatk --scatter` through the CLI: its --json result manifest and log."""
    try:
        main(["vcf", "gatk", "--scatter", "--input", ultima.bam, "--ref", ultima.ref,
              "--outdir", str(tmp_path / "out"), *extra, "--json"])  # fmt: skip
    except SystemExit:
        pass
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.parametrize("custom_temp", [False, True], ids=["outdir", "tmp-dir"])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_scatter_gathers_shards_in_reference_order(
    ultima, gatk, tmp_path, monkeypatch, capsys, custom_temp, platform
):
    """chr2's shard finishes last but is gathered first. On Windows each worker
    gets native paths; with --tmp-dir on another volume the gathered VCF is
    published from beside the output, not moved across volumes."""
    monkeypatch.setattr(paths, "sys", Namespace(platform=platform))
    gatk.set(windows=platform == "win32", on_contig={"chr2": "slow"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    replace = Path.replace

    def other_volume(source, target):
        if Path(source).is_relative_to(scratch) != Path(target).is_relative_to(scratch):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", other_volume)
    temp = ["--tmp-dir", str(scratch)] if custom_temp else []
    result, log = _scatter(capsys, ultima, tmp_path, "--threads", "2", *temp)

    assert result["status"] == "ok", result["error"]
    output = tmp_path / "out" / "gatk.vcf.gz"
    assert vcf_calls(output) == ultima.calls
    assert vcf_calls(output, "chr1") == ultima.calls[1:]  # the index is current
    assert "can change annotations and genotypes" in log
    runs = gatk.runs()
    assert len(runs) == 2
    for argv in runs:
        # contract: the preflight budgets one CPU and a 6 GiB JVM per worker.
        assert "-Xmx4g" in argv[argv.index("--java-options") + 1]
        assert argv[argv.index("--native-pair-hmm-threads") + 1] == "1"
        assert argv[argv.index("--flow-mode") + 1] == "ADVANCED"
    assert sorted(p.name for p in output.parent.iterdir()) == [
        "gatk.vcf.gz",
        "gatk.vcf.gz.tbi",
        "gatk.vcf.manifest.json",
    ]
    assert list(scratch.iterdir()) == []


def test_scatter_that_fits_one_worker_runs_one_gatk(ultima, gatk, tmp_path, capsys):
    result, log = _scatter(capsys, ultima, tmp_path, "--threads", "1")
    assert result["status"] == "ok", result["error"]
    assert vcf_calls(tmp_path / "out" / "gatk.vcf.gz") == ultima.calls
    assert len(gatk.runs()) == 1
    assert "using the unscattered path" in log


def test_scatter_requires_flow_aware_input(ultima, gatk, tmp_path, capsys):
    illumina = Namespace(ref=ultima.ref, bam=ultima.illumina)
    result, _log = _scatter(capsys, illumina, tmp_path, "--threads", "2")
    assert result["status"] == "failed"
    assert "requires flow-aware Ultima" in result["error"]["message"]
    assert gatk.runs() == []


def test_worker_failure_stops_peers_and_preserves_existing_output(
    ultima, gatk, tmp_path, capsys
):
    """chr1's worker and its Java child are stopped when chr2's fails; the
    previous VCF and index stay, and nothing is gathered."""
    gatk.set(on_contig={"chr2": "fail", "chr1": "hang"})
    output = tmp_path / "out" / "gatk.vcf.gz"
    output.parent.mkdir()
    output.write_text("existing output")
    Path(f"{output}.tbi").write_text("existing index")
    started = time.monotonic()
    result, _log = _scatter(capsys, ultima, tmp_path, "--threads", "2")

    assert result["status"] == "failed"
    assert "chr2 failed" in result["error"]["message"]
    assert time.monotonic() - started < 30
    launcher, java = map(int, gatk.pids.read_text().split())
    assert _gone(launcher) and _gone(java)
    assert output.read_text() == "existing output"
    assert Path(f"{output}.tbi").read_text() == "existing index"


def test_gatk_scatter_rejects_gpu_before_any_work(gatk, tmp_path):
    with pytest.raises(UsageError, match="cannot be combined"):
        run_wgsextract("vcf", "gatk", "--scatter", "--gpu", "--input",
                       str(tmp_path / "missing.bam"))  # fmt: skip
    assert gatk.runs() == []


def test_atomic_scatter_publishes_an_index_of_the_new_vcf(
    ultima, gatk, tmp_path, capsys
):
    output = tmp_path / "out" / "gatk.vcf.gz"
    output.parent.mkdir()
    output.write_text("previous VCF")
    Path(f"{output}.tbi").write_text("previous index")
    result, _log = _scatter(
        capsys, ultima, tmp_path, "--threads", "2", "--atomic-output"
    )
    assert result["status"] == "ok", result["error"]
    assert vcf_calls(output, "chr1") == ultima.calls[1:]


def test_scatter_preflight_counts_all_worker_heaps(tmp_path):
    estimate = resource_estimates.estimate_resources(
        "gatk", input_paths=[], output_dir=str(tmp_path), threads="2", gatk_workers=2
    )
    assert estimate.resources[0].components == {"caller": 12 * 1024**3}


def test_scatter_preflight_charges_staging_to_output_not_java_volume(
    tmp_path, monkeypatch
):
    mib = 1024**2
    volumes = Volumes(monkeypatch)
    results, java = tmp_path / "results", tmp_path / "java-temp"
    results.mkdir()
    java.mkdir()
    volumes.free[results] = 15 * mib / GIB
    volumes.free[java] = 100
    sample = tmp_path / "sample.bam"
    sample.write_bytes(b"BAM")
    volumes.sizes[sample] = 100 * mib
    estimate = resource_estimates.estimate_resources(
        "gatk",
        input_paths=[str(sample)],
        output_dir=str(results),
        temp_dir=str(java),
        threads="2",
        gatk_workers=2,
    )
    output_disk, java_disk = estimate.resources[1:]
    assert output_disk.status == "LOW"
    assert output_disk.components["scatter_parts"] == 10 * mib
    assert output_disk.components["scatter_indexes"] > 0
    assert output_disk.paths == (str(results),)
    assert java_disk.components == {"temporary": 20 * mib}
    assert java_disk.status == "OK"
