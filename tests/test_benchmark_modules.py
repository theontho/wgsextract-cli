"""Benchmark building blocks: dataset download/cache/checksum/extract, CLI step
execution and reporting, fixtures, and machine stats. Nothing touches the
network; the release dataset zip is rebuilt locally in its published layout
(see scripts/build_benchmark_dataset.py)."""

import gzip
import hashlib
import io
import json
import os
import sys
import urllib.error
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli.commands import benchmark as benchmark_entry
from wgsextract_cli.commands.benchmark import (
    datasets,
    environment,
    execution,
    fixtures,
    machine,
    setup,
)
from wgsextract_cli.commands.benchmark.models import (
    BenchmarkDerivedAlignment,
    BenchmarkRemoteFile,
    BenchmarkResult,
    BenchmarkThreadPlan,
)
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

DATASET_DIR = "wgsextract-benchmark-hg19-mini"
DATASET_FILES = {
    "ref": "hg19-mini.fa.gz",
    "ref_fai": "hg19-mini.fa.gz.fai",
    "bam": "HG00096.hg19-mini.bam",
    "bam_index": "HG00096.hg19-mini.bam.bai",
    "fastq_r1": "HG00096.hg19-mini_R1.fastq.gz",
    "fastq_r2": "HG00096.hg19-mini_R2.fastq.gz",
    "targets": "HG00096.hg19-mini.targets.tab.gz",
    "targets_index": "HG00096.hg19-mini.targets.tab.gz.tbi",
}


def _dataset_zip(path: Path, manifest_extra: dict | None = None) -> Path:
    """A zip shaped like the release asset: one folder holding manifest.json
    and the files it names."""
    manifest = {
        "dataset_id": "hg19-mini-hg00096",
        "description": "HG00096 mini",
        "sample": "HG00096",
        "build": "hg19",
        "default_region": "20",
        "files": DATASET_FILES,
        **(manifest_extra or {}),
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{DATASET_DIR}/manifest.json", json.dumps(manifest))
        for name in DATASET_FILES.values():
            body = "20\t1000\t4\t60\t61\nMT\t16569\t1025\t60\t61\n"
            zf.writestr(
                f"{DATASET_DIR}/{name}", body if name.endswith(".fai") else name
            )
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Response(io.BytesIO):
    def info(self) -> dict[str, str]:
        return {"Content-Length": str(len(self.getvalue()))}


@pytest.fixture
def served(monkeypatch):
    """Serve URL -> bytes from memory instead of the network; log each fetch."""
    files: dict[str, bytes] = {}
    fetched: list[str] = []

    def urlopen(request, timeout):
        fetched.append(request.full_url)
        if request.full_url not in files:
            raise urllib.error.URLError("404")
        return _Response(files[request.full_url])

    monkeypatch.setattr(datasets.urllib.request, "urlopen", urlopen)
    return files, fetched


# --- datasets: download, cache, checksum ----------------------------------


def test_download_file_writes_atomically_and_cleans_partial_on_error(
    tmp_path, served, monkeypatch
):
    files, _ = served
    files["https://example.test/a.zip"] = b"payload"
    dest = tmp_path / "a.zip"
    datasets._download_file("https://example.test/a.zip", dest)
    assert dest.read_bytes() == b"payload"
    assert not (tmp_path / "a.zip.tmp").exists()

    def broken(response, handle, progress_label):
        handle.write(b"half")
        raise OSError("connection reset")

    monkeypatch.setattr(datasets, "copy_response_to_file", broken)
    with pytest.raises(WGSExtractError, match="b.zip: connection reset"):
        datasets._download_file("https://example.test/a.zip", tmp_path / "b.zip")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.zip"]


def test_cached_remote_file_downloads_verifies_and_then_trusts_marker(
    tmp_path, served, monkeypatch
):
    files, fetched = served
    url = "https://example.test/sample.cram"
    files[url] = b"cram bytes"
    remote = BenchmarkRemoteFile(
        "bam", url, "sample.cram", hashlib.md5(b"cram bytes").hexdigest().upper()
    )

    path = datasets._cached_remote_dataset_file(remote, tmp_path)
    assert path.read_bytes() == b"cram bytes"
    marker = datasets._verified_checksum_path(path, remote.md5)
    assert marker.name == f".sample.cram.{remote.md5.lower()}.verified"
    assert marker.read_text().splitlines()[0] == remote.md5.lower()

    monkeypatch.setattr(datasets, "_file_digest", lambda *a: pytest.fail("rehashed"))
    assert datasets._cached_remote_dataset_file(remote, tmp_path) == path
    assert fetched == [url]


def test_cached_remote_file_without_md5_reuses_any_existing_file(tmp_path, served):
    _, fetched = served
    (tmp_path / "ref.fa").write_text("old")
    remote = BenchmarkRemoteFile("ref", "https://example.test/ref.fa", "ref.fa")
    assert datasets._cached_remote_dataset_file(remote, tmp_path).read_text() == "old"
    assert fetched == []


def test_cached_remote_file_rejects_download_with_wrong_md5(tmp_path, served):
    files, _ = served
    url = "https://example.test/sample.cram"
    files[url] = b"<html>proxy error</html>"
    remote = BenchmarkRemoteFile("bam", url, "sample.cram", "0" * 32)
    with pytest.raises(WGSExtractError, match="MD5 mismatch"):
        datasets._cached_remote_dataset_file(remote, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cached_remote_file_refetches_after_a_corrupt_download(tmp_path, served):
    files, fetched = served
    url = "https://example.test/sample.cram"
    good = b"cram bytes"
    remote = BenchmarkRemoteFile(
        "bam", url, "sample.cram", hashlib.md5(good).hexdigest()
    )
    files[url] = b"truncated"
    with pytest.raises(WGSExtractError):
        datasets._cached_remote_dataset_file(remote, tmp_path)
    files[url] = good
    assert datasets._cached_remote_dataset_file(remote, tmp_path).read_bytes() == good
    assert fetched == [url, url]

    # A cached file that has since gone bad is replaced once.
    (tmp_path / "sample.cram").write_bytes(b"bit rot")
    assert datasets._cached_remote_dataset_file(remote, tmp_path).read_bytes() == good
    assert fetched == [url, url, url]


def test_cached_remote_file_drops_corrupt_dev_cache_copy_and_downloads(
    tmp_path, served, monkeypatch
):
    files, fetched = served
    url = "https://example.test/sample.cram"
    files[url] = b"good"
    remote = BenchmarkRemoteFile(
        "bam", url, "sample.cram", hashlib.md5(b"good").hexdigest()
    )
    dropped = []

    def restore(url, path, checksum_hint):
        path.write_bytes(b"stale")
        return path

    monkeypatch.setattr(datasets, "restore_cached_download", restore)
    monkeypatch.setattr(
        datasets, "drop_cached_download", lambda *a, **k: dropped.append(a[0])
    )
    assert (
        datasets._cached_remote_dataset_file(remote, tmp_path).read_bytes() == b"good"
    )
    assert dropped == [url] and fetched == [url]


def test_real_dataset_cache_dir_precedence(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setitem(settings, "dev_download_cache", False)
    assert (
        datasets._real_dataset_cache_dir(Namespace(dataset_cache_dir=None), out)
        == out / "datasets"
    )
    explicit = Namespace(dataset_cache_dir=str(tmp_path / "x" / ".." / "cache"))
    assert datasets._real_dataset_cache_dir(explicit, out) == tmp_path / "cache"
    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setattr(
        datasets, "benchmark_dataset_cache_root", lambda: tmp_path / "dev"
    )
    assert datasets._real_dataset_cache_dir(Namespace(), out) == tmp_path / "dev"


def test_dataset_sha256_and_filename_normalisation():
    assert datasets._normalized_dataset_sha256(Namespace(dataset_sha256=" AB ")) == "ab"
    assert datasets._normalized_dataset_sha256(Namespace(dataset_sha256="  ")) is None
    assert datasets._normalized_dataset_sha256(Namespace()) is None
    assert datasets._download_filename("https://h/d/x.zip?sig=1") == "x.zip"
    with pytest.raises(WGSExtractError, match="must end with a filename"):
        datasets._download_filename("")


def test_download_filename_rejects_url_ending_in_slash():
    with pytest.raises(WGSExtractError, match="must end with a filename"):
        datasets._download_filename("https://example.test/releases/")
    with pytest.raises(WGSExtractError, match="must end with a filename"):
        datasets._download_filename("https://example.test")


# --- datasets: manifest loading -------------------------------------------


def _write_dataset(root: Path, files: dict[str, str], **manifest) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in filter(None, files.values()):
        (root / name).write_text("x")
    (root / "manifest.json").write_text(json.dumps({"files": files, **manifest}))
    return root


def test_load_dataset_finds_single_nested_manifest_and_defaults(tmp_path):
    root = _write_dataset(
        tmp_path / "zipdir", {"ref": "r.fa", "bam": "s.bam", "vcf": ""}
    )
    dataset = datasets._load_real_benchmark_dataset(tmp_path)
    assert dataset.root == root
    assert (dataset.dataset_id, dataset.build) == ("zipdir", "hg19")
    assert (dataset.default_region, dataset.region_safe, dataset.vcf) == (
        None,
        False,
        None,
    )
    assert datasets._existing_dataset_outputs(dataset) == [
        root / "r.fa",
        root / "s.bam",
    ]


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"ref": "r.fa"}, "missing required file role: bam"),
        ({"ref": "r.fa", "bam": "../s.bam"}, "escapes dataset root: bam"),
        (
            {"ref": "r.fa", "bam": "s.bam", "fastq_r1": "1.fq"},
            "fastq_r1 without fastq_r2",
        ),
    ],
)
def test_load_dataset_rejects_bad_manifests(tmp_path, files, error):
    (tmp_path / "s.bam").write_text("x")
    _write_dataset(tmp_path / "d", files)
    with pytest.raises(WGSExtractError, match=error):
        datasets._load_real_benchmark_dataset(tmp_path / "d")


def test_load_dataset_reports_missing_files_and_manifest(tmp_path):
    with pytest.raises(WGSExtractError, match="manifest not found"):
        datasets._load_real_benchmark_dataset(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"files": ["r.fa"]}))
    with pytest.raises(WGSExtractError, match="no files object"):
        datasets._load_real_benchmark_dataset(tmp_path)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"files": {"ref": "r.fa", "bam": "s.bam"}})
    )
    with pytest.raises(WGSExtractError, match=r"incomplete.*r\.fa, .*s\.bam"):
        datasets._load_real_benchmark_dataset(tmp_path)


# --- archive dataset: zip checksum, cache and extraction ------------------


def test_real_archive_downloads_once_verifies_and_extracts(
    tmp_path, served, monkeypatch
):
    monkeypatch.setitem(settings, "dev_download_cache", False)
    files, fetched = served
    asset = _dataset_zip(tmp_path / "asset.zip")
    url = "https://example.test/rel/wgsextract-benchmark-hg19-mini.zip?x=1"
    files[url] = asset.read_bytes()
    args = Namespace(
        dataset="real",
        dataset_zip=None,
        dataset_url=url,
        dataset_sha256=_sha256(asset).upper(),
        dataset_cache_dir=None,
    )

    dataset = environment._prepare_real_benchmark_dataset(
        args, tmp_path / "run1", tmp_path
    )
    assert dataset.root == tmp_path / "run1" / "real" / DATASET_DIR
    assert dataset.bam.read_text() == DATASET_FILES["bam"]
    assert dataset.targets_index.name == DATASET_FILES["targets_index"]
    assert (dataset.dataset_id, dataset.build, dataset.default_region) == (
        "hg19-mini-hg00096",
        "hg19",
        "20",
    )
    cached = tmp_path / "datasets" / "wgsextract-benchmark-hg19-mini.zip"
    assert cached.read_bytes() == asset.read_bytes()

    environment._prepare_real_benchmark_dataset(args, tmp_path / "run2", tmp_path)
    assert fetched == [url]

    cached.write_bytes(b"corrupt")
    environment._prepare_real_benchmark_dataset(args, tmp_path / "run3", tmp_path)
    assert fetched == [url, url] and cached.read_bytes() == asset.read_bytes()

    files[url] = b"tampered"
    cached.unlink()
    with pytest.raises(WGSExtractError, match="SHA256 mismatch"):
        environment._prepare_real_benchmark_dataset(args, tmp_path / "run4", tmp_path)
    assert not (tmp_path / "run4" / "real").exists()


def test_real_archive_without_checksum_reuses_cached_zip(tmp_path, served):
    files, fetched = served
    url = "https://example.test/d.zip"
    files[url] = _dataset_zip(tmp_path / "a.zip").read_bytes()
    args = Namespace(
        dataset_url=url, dataset_sha256="", dataset_cache_dir=str(tmp_path / "c")
    )
    first = environment._real_dataset_zip_path(args, tmp_path)
    assert (
        environment._real_dataset_zip_path(args, tmp_path)
        == first
        == tmp_path / "c" / "d.zip"
    )
    assert fetched == [url]


def test_real_archive_restores_from_dev_cache_without_downloading(
    tmp_path, served, monkeypatch
):
    files, fetched = served
    monkeypatch.setitem(settings, "dev_download_cache", True)
    url = "https://example.test/d.zip"
    files[url] = _dataset_zip(tmp_path / "a.zip").read_bytes()
    args = Namespace(
        dataset_url=url,
        dataset_sha256=_sha256(tmp_path / "a.zip"),
        dataset_cache_dir=str(tmp_path / "c"),
    )
    zip_path = environment._real_dataset_zip_path(args, tmp_path)
    zip_path.unlink()
    assert environment._real_dataset_zip_path(args, tmp_path).read_bytes() == files[url]
    assert fetched == [url]


def test_local_dataset_zip_is_checked_and_missing_one_rejected(tmp_path):
    asset = _dataset_zip(tmp_path / "local.zip")
    args = Namespace(dataset="real", dataset_zip=str(asset), dataset_sha256=None)
    dataset = environment._prepare_real_benchmark_dataset(
        args, tmp_path / "run", tmp_path
    )
    assert dataset.ref.name == DATASET_FILES["ref"]
    args.dataset_zip = str(tmp_path / "nope.zip")
    with pytest.raises(WGSExtractError, match="zip not found"):
        environment._real_dataset_zip_path(args, tmp_path)


@pytest.mark.parametrize("entry", ["../evil.txt", "/abs/evil.txt", "d/../../evil.txt"])
def test_extract_zip_refuses_entries_outside_the_target(tmp_path, entry):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("ok.txt", "fine")
        zf.writestr(entry, "evil")
    with pytest.raises(WGSExtractError, match="Unsafe benchmark dataset zip entry"):
        environment._extract_zip_safely(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "out" / "ok.txt").exists()


def test_extract_zip_reports_non_zip(tmp_path):
    (tmp_path / "x.zip").write_text("not a zip")
    with pytest.raises(WGSExtractError, match="Invalid benchmark dataset zip"):
        environment._extract_zip_safely(tmp_path / "x.zip", tmp_path / "out")


@needs_htslib
def test_derived_alignment_subsamples_to_indexed_cram_and_is_reused(
    tmp_path, monkeypatch
):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 250})
    lines = ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chr1\tLN:1000"]
    lines += [
        f"r{i}\t0\tchr1\t{1 + i * 4}\t60\t20M\t*\t0\t0\t{('ACGT' * 5)}\t{'I' * 20}"
        for i in range(200)
    ]
    source = Path(sam_to_bam(lines, tmp_path / "src.bam"))
    spec = BenchmarkDerivedAlignment("source_cram", "sub.cram", "sub.crai", "11.25")
    cached = {"source_cram": source, "ref": ref}
    datasets._prepare_derived_alignment(spec, cached, tmp_path)
    assert (
        cached["bam"] == tmp_path / "sub.cram"
        and cached["bam_index"] == tmp_path / "sub.crai"
    )
    assert not (tmp_path / "sub.cram.crai").exists()
    kept = int(
        run_command(
            [
                get_tool_path("samtools"),
                "view",
                "-c",
                "-T",
                str(ref),
                str(cached["bam"]),
            ],
            capture_output=True,
        ).stdout
    )
    assert 20 < kept < 100

    monkeypatch.setattr(datasets, "run_command", lambda *a, **k: pytest.fail("rebuilt"))
    datasets._prepare_derived_alignment(
        spec, {"source_cram": source, "ref": ref}, tmp_path
    )
    with pytest.raises(WGSExtractError, match="requires source alignment and ref"):
        datasets._prepare_derived_alignment(spec, {"ref": ref}, tmp_path)


# --- execution -------------------------------------------------------------


def test_cli_command_carries_flags_threads_and_memory(monkeypatch):
    monkeypatch.setattr(execution, "config_args", lambda: ["--config", "/c.toml"])
    plan = BenchmarkThreadPlan("3", 3, "test")
    args = Namespace(
        debug=True, quiet=True, memory="2G", _benchmark_thread_plan=plan, threads=9
    )
    cmd = execution._cli_command(
        args, ["bam", "sort"], execution._benchmark_threads(args)
    )
    assert cmd[:3] == [sys.executable, "-m", "wgsextract_cli.main"]
    assert cmd[5:] == [
        "--config",
        "/c.toml",
        "--debug",
        "bam",
        "sort",
        "--threads",
        "3",
        "--memory",
        "2G",
    ]
    quiet = Namespace(quiet=True, threads=None)
    assert execution._cli_command(quiet, ["info"], execution._benchmark_threads(quiet))[
        -2:
    ] == ["--quiet", "info"]


def test_subprocess_env_puts_this_source_tree_first(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    src = str(Path(benchmark_entry.__file__).resolve().parents[2])
    assert execution._subprocess_env()["PYTHONPATH"].split(os.pathsep) == [
        src,
        "/elsewhere",
    ]


def _script(monkeypatch, code: str) -> None:
    monkeypatch.setattr(
        execution, "_cli_command", lambda *a: [sys.executable, "-c", code]
    )


def test_cli_step_passes_logs_output_and_label(tmp_path, monkeypatch):
    out = tmp_path / "step" / "o.txt"
    _script(
        monkeypatch,
        f"import sys; open({str(out)!r}, 'w').write('x'); print('hi'); print('warn', file=sys.stderr)",
    )
    result = execution._run_cli_step(
        Namespace(),
        "Sort",
        "05-sort",
        ["bam", "sort", "--x"],
        out.parent,
        tmp_path / "logs",
        [out],
    )
    assert (result.name, result.status, result.returncode, result.error) == (
        "Sort [bam sort]",
        "PASS",
        0,
        None,
    )
    assert Path(result.stdout_log).read_text() == "hi\n"
    assert Path(result.stderr_log).read_text() == "warn\n"
    assert result.seconds > 0 and result.expected_outputs == [str(out)]


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("raise SystemExit(3)", "Command exited with status 3."),
        ("pass", "Missing expected output(s): "),
    ],
)
def test_cli_step_fails_on_exit_status_or_missing_output(
    tmp_path, monkeypatch, code, error
):
    _script(monkeypatch, code)
    result = execution._run_cli_step(
        Namespace(), "N", "s", [], tmp_path, tmp_path, [tmp_path / "gone"]
    )
    assert result.status == "FAIL" and result.error.startswith(error)
    assert result.name == "N"


def test_pipe_step_streams_file_through_the_command(tmp_path, monkeypatch):
    _script(
        monkeypatch,
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())",
    )
    src, dst = tmp_path / "in.txt", tmp_path / "out" / "o.txt"
    src.write_bytes(b"acgt\n")
    dst.parent.mkdir()
    result = fixtures._run_cli_pipe_step(
        Namespace(),
        "Up",
        "up",
        ["repair", "x"],
        src,
        dst,
        dst.parent,
        tmp_path / "l",
        [dst],
    )
    assert result.status == "PASS" and dst.read_bytes() == b"ACGT\n"
    assert Path(result.stdout_log).read_text() == f"{dst}\n"


@needs_htslib
def test_pipe_step_runs_real_ftdna_vcf_repair_on_benchmark_fixture(tmp_path):
    sam, vcf = tmp_path / "fx" / "bad.sam", tmp_path / "fx" / "bad.vcf"
    fixtures._prepare_repair_fixtures(sam, vcf)
    out = tmp_path / "fixed.vcf"
    result = fixtures._run_cli_pipe_step(
        Namespace(threads=None),
        "Repair",
        "18c",
        ["repair", "ftdna-vcf"],
        vcf,
        out,
        tmp_path,
        tmp_path / "l",
        [out],
    )
    assert result.status == "PASS", Path(result.stderr_log).read_text()
    assert "PASS!BAD" in vcf.read_text()
    record = out.read_text().splitlines()[-1].split("\t")
    assert record[:2] == ["chr1", "1"] and record[6] != "PASS!BAD"


def test_internal_step_combines_exception_and_missing_outputs(tmp_path):
    def boom():
        raise RuntimeError("bad input")

    result = execution._run_internal_step(
        "I", "i", tmp_path / "o", boom, [tmp_path / "m"]
    )
    assert result.status == "FAIL"
    assert result.error == f"bad input; Missing expected output(s): {tmp_path / 'm'}"
    assert result.command == ["internal", "i"] and (tmp_path / "o").is_dir()
    ok = execution._run_internal_step("I", "i", tmp_path, lambda: None, [])
    assert (ok.status, ok.error, ok.success) == ("PASS", None, True)


def test_skip_results_and_tool_activity(monkeypatch):
    skipped = execution._missing_optional_tool_result("fastp", "20-fastp", Path("/o"))
    assert (skipped.status, skipped.seconds, skipped.command) == ("SKIP", 0.0, [])
    assert "fastp" in skipped.name and "fastp" in skipped.error
    assert not execution._tool_active_for_benchmark("bwa", None)
    monkeypatch.setattr(execution.platform, "system", lambda: "Darwin")
    assert not execution._tool_active_for_benchmark("sambamba", "/bin/sambamba")
    assert execution._tool_active_for_benchmark("samtools", "/bin/samtools")


# --- run(): failure handling and report ------------------------------------


def _run_args(tmp_path: Path, **overrides) -> Namespace:
    values = {
        "profile": "smoke",
        "dataset": "fake",
        "coverage": None,
        "full_size": False,
        "build": "hg38",
        "seed": 1,
        "target_count": None,
        "suite": "core",
        "region": None,
        "keep_going": False,
        "outdir": str(tmp_path),
        "_explicit_dests": {"outdir"},
        "debug": False,
        "quiet": False,
        "threads": 2,
        "memory": None,
        "dataset_zip": None,
        "dataset_sha256": None,
        "dataset_cache_dir": None,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.fixture
def stub_steps(monkeypatch):
    """Replace CLI steps with instant ones that fail for slugs in ``failing``."""
    calls: dict[str, list[str]] = {}
    failing: set[str] = set()

    def cli_step(
        args,
        name,
        slug,
        command_args,
        output_dir,
        logs_dir,
        expected_outputs,
        command_label=None,
    ):
        calls[slug] = command_args
        logs_dir.mkdir(parents=True, exist_ok=True)
        stderr = logs_dir / f"{slug}.stderr.log"
        stderr.write_text(f"{slug} went wrong\n" if slug in failing else "")
        for path in expected_outputs:
            if slug not in failing:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
        status = "FAIL" if slug in failing else "PASS"
        return BenchmarkResult(
            name,
            slug,
            status,
            0.01,
            ["x", slug],
            str(output_dir),
            stderr_log=str(stderr),
            returncode=int(slug in failing),
        )

    def target_tab(ref, tab, count, region):
        if "02-microarray-targets" in failing:
            raise WGSExtractError(
                f"No reference contigs match benchmark region: {region}"
            )
        tab.touch()
        Path(f"{tab}.tbi").touch()

    for module in (setup, benchmark_entry.core):
        monkeypatch.setattr(module, "_run_cli_step", cli_step)
    monkeypatch.setattr(setup, "_create_target_snp_tab", target_tab)
    monkeypatch.setattr(benchmark_entry, "_benchmark_external_tools", lambda: [])
    monkeypatch.setattr(
        benchmark_entry,
        "_machine_stats",
        lambda run_dir: {
            "physical_cores": None,
            "logical_cores": None,
            "ram_total": None,
            "ram_available": None,
            "disk_total": None,
            "disk_free": None,
            "drive_model": None,
        },
    )
    return calls, failing


def _report(tmp_path: Path) -> dict:
    (path,) = tmp_path.glob("runs/*/benchmark_results.json")
    return json.loads(path.read_text())


def test_failed_step_stops_run_and_still_writes_report(tmp_path, stub_steps, capsys):
    _calls, failing = stub_steps
    failing.add("03-bam-unalign")
    with pytest.raises(
        WGSExtractError, match=r"BAM unalignment .*03-bam-unalign\.stderr\.log"
    ):
        benchmark_entry.run(_run_args(tmp_path))
    out = capsys.readouterr().out
    assert "03-bam-unalign went wrong" in out
    slugs = [r["slug"] for r in _report(tmp_path)["results"]]
    assert slugs[-1] == "03-bam-unalign" and "03b-align" not in slugs
    assert "Failed: 1" in out


def test_internal_step_failure_message_shows_its_error(tmp_path, stub_steps):
    _calls, failing = stub_steps
    failing.add("02-microarray-targets")
    with pytest.raises(WGSExtractError) as excinfo:
        benchmark_entry.run(_run_args(tmp_path))
    assert "No reference contigs match" in str(excinfo.value)
    assert "See None" not in str(excinfo.value)


def test_keep_going_runs_remaining_steps_after_failure(tmp_path, stub_steps):
    calls, failing = stub_steps
    failing.add("03-bam-unalign")
    with pytest.raises(WGSExtractError):
        benchmark_entry.run(_run_args(tmp_path, keep_going=True))
    results = _report(tmp_path)["results"]
    assert [r["slug"] for r in results if r["status"] == "FAIL"] == ["03-bam-unalign"]
    assert results[-1]["slug"] == list(calls)[-1]
    assert "04a-bam-subset" in calls


def test_keep_going_still_reports_failure_at_the_end(tmp_path, stub_steps):
    _calls, failing = stub_steps
    failing.add("04b-mt-bam")
    failing.add("03-bam-unalign")
    with pytest.raises(WGSExtractError) as excinfo:
        benchmark_entry.run(_run_args(tmp_path, keep_going=True))
    assert str(excinfo.value) == (
        "2 benchmark step(s) failed: BAM unalignment to paired FASTQ; "
        "Mitochondrial BAM extraction"
    )


def test_real_smoke_run_uses_archive_files_and_records_base_size(
    tmp_path, stub_steps, capsys
):
    calls, _ = stub_steps
    asset = _dataset_zip(
        tmp_path / "asset.zip", {"default_region": "chrM:1-500", "region_safe": True}
    )
    benchmark_entry.run(
        _run_args(
            tmp_path / "o",
            dataset="real",
            dataset_zip=str(asset),
            dataset_sha256=_sha256(asset),
        )
    )

    report = _report(tmp_path / "o")
    meta, results = report["metadata"], report["results"]
    root = Path(meta["run_dir"]) / "dataset" / "real" / DATASET_DIR
    assert meta["data_source"] == "real" and meta["build"] == "hg19"
    assert meta["region"] == "MT:1-500"
    assert meta["base_file"] == str(root / DATASET_FILES["bam"])
    assert meta["base_file_size_bytes"] == len(DATASET_FILES["bam"])
    assert [r["slug"] for r in results[:4]] == [
        "00-load-real-dataset",
        "00a-info-detailed",
        "01-reference-index",
        "02-microarray-targets",
    ]
    assert results[3]["expected_outputs"] == [
        str(root / DATASET_FILES["targets"]),
        str(root / DATASET_FILES["targets_index"]),
    ]
    assert all(r["status"] == "PASS" for r in results)
    assert calls["03-bam-unalign"][-2:] == ["--region", "MT:1-500"]
    assert "00-generate-bam" not in calls
    md = (Path(meta["run_dir"]) / "benchmark_report.md").read_text()
    assert "- Data source: `real` (HG00096 mini)" in md
    assert (
        f"Benchmark base file: {root / DATASET_FILES['bam']} ({len(DATASET_FILES['bam'])} bytes)"
        in capsys.readouterr().out
    )


def test_unsafe_real_dataset_ignores_region(tmp_path, stub_steps):
    calls, _ = stub_steps
    asset = _dataset_zip(tmp_path / "asset.zip")
    benchmark_entry.run(
        _run_args(tmp_path / "o", dataset="real", dataset_zip=str(asset), region="MT")
    )
    assert _report(tmp_path / "o")["metadata"]["region"] == "MT"
    assert "--region" not in calls["03-bam-unalign"]
    assert "--region" not in calls["04a-bam-subset"]


@pytest.mark.parametrize(("flag", "value"), [("coverage", 0.0), ("target_count", -1)])
def test_run_rejects_non_positive_sizes(tmp_path, flag, value):
    with pytest.raises(WGSExtractError, match="must be greater than zero"):
        benchmark_entry.run(_run_args(tmp_path, **{flag: value}))
    assert not (tmp_path / "runs").exists()


# --- fixtures --------------------------------------------------------------


@needs_htslib
def test_trio_inputs_are_renamed_copies_with_indexes(tmp_path):
    plain = tmp_path / "src.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chrM,length=100>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="GT">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        "chrM\t5\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\n"
    )
    source = Path(bgzip_index(plain))
    outputs = {
        name: tmp_path / f"{name}.vcf.gz" for name in ("child", "mother", "father")
    }
    fixtures._prepare_trio_vcf_inputs(source, outputs)
    bcftools = get_tool_path("bcftools")
    for name, vcf in outputs.items():
        assert (
            run_command([bcftools, "query", "-l", str(vcf)], capture_output=True).stdout
            == f"{name}\n"
        )
        assert Path(f"{vcf}.tbi").exists()
        assert (
            "chrM\t5"
            in run_command(
                [bcftools, "view", "-H", str(vcf)], capture_output=True
            ).stdout
        )


def test_trio_region_prefers_mito_then_first_contig(tmp_path):
    ref = tmp_path / "r.fa"
    assert fixtures._trio_benchmark_region(ref) is None
    Path(f"{ref}.fai").write_text("chr1\t250000\t6\t60\t61\nchrM\t16569\t1\t60\t61\n")
    assert fixtures._trio_benchmark_region(ref) == "chrM:1-16569"
    Path(f"{ref}.fai").write_text("chr1\t250000\t6\t60\t61\n")
    assert fixtures._trio_benchmark_region(ref) == "chr1:1-100000"


def test_analyze_batch_fixture_creates_sample_files(tmp_path):
    fixtures._prepare_analyze_batch_fixture(tmp_path / "b")
    assert sorted(p.name for p in (tmp_path / "b").iterdir()) == [
        "benchmark_sample.bam",
        "benchmark_sample.vcf.gz",
        "benchmark_sample.vcf.gz.tbi",
    ]


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        (None, [("chr1", 1, 1000), ("chrM", 1, 50)]),
        ("1:1,0-2,000", [("chr1", 10, 1000)]),
        ("chrM:40", [("chrM", 40, 50)]),
        ("chrM:0-10", [("chrM", 1, 10)]),
        ("chrM:60-70", []),
        ("chr9", []),
    ],
)
def test_target_ranges_clamp_to_contigs(region, expected):
    assert fixtures._target_ranges([("chr1", 1000), ("chrM", 50)], region) == expected


@needs_htslib
def test_target_tab_has_requested_count_inside_region(tmp_path):
    ref = write_fasta(tmp_path / "r.fa", {"chr1": "A" * 5000, "chrM": "C" * 300})
    tab = tmp_path / "t.tab.gz"
    fixtures._create_target_snp_tab(ref, tab, 25, "chr1:1001-2000")
    assert Path(f"{tab}.tbi").exists() and not (tmp_path / "t.tab").exists()
    with gzip.open(tab, "rt") as handle:
        rows = [line.split("\t") for line in handle.read().splitlines()]
    assert rows[0] == ["#CHROM", "POS", "ID", "REF", "ALT"]
    body = rows[1:]
    assert len(body) == 25
    positions = [int(r[1]) for r in body]
    assert (
        positions == sorted(set(positions))
        and 1001 <= positions[0]
        and positions[-1] <= 2000
    )
    assert all(r[0] == "chr1" and r[3] != r[4].strip() for r in body)
    assert [r[2] for r in body] == [f"bench_rs{i}" for i in range(1, 26)]


@needs_htslib
def test_target_tab_spreads_over_whole_genome_and_indexes_missing_fai(tmp_path):
    ref = tmp_path / "r.fa"
    ref.write_text(">chr1\n" + "A" * 3000 + "\n>chrM\n" + "C" * 1000 + "\n")
    tab = tmp_path / "t.tab.gz"
    fixtures._create_target_snp_tab(ref, tab, 40, None)
    assert Path(f"{ref}.fai").exists()
    with gzip.open(tab, "rt") as handle:
        chroms = [line.split("\t")[0] for line in handle.read().splitlines()[1:]]
    assert 30 <= chroms.count("chr1") <= 31 and 10 <= chroms.count("chrM") <= 11
    with pytest.raises(WGSExtractError, match="No reference contigs match"):
        fixtures._create_target_snp_tab(ref, tab, 10, "chrY")


# --- machine ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1023, "1,023 bytes"),
        (1536, "1.5 KiB (1,536 bytes)"),
        (5 * 1024**3, "5.0 GiB (5,368,709,120 bytes)"),
        (3 * 1024**5, "3072.0 TiB (3,377,699,720,527,872 bytes)"),
    ],
)
def test_format_bytes(size, text):
    assert machine._format_bytes(size) == text


def test_machine_stats_on_this_host_are_complete(tmp_path):
    stats = machine._machine_stats(tmp_path)
    assert set(stats) >= {
        "os",
        "python",
        "cpu_model",
        "ram_total",
        "disk_free",
        "drive_speed",
    }
    assert isinstance(stats["logical_cores"], int) and stats["logical_cores"] >= 1
    assert stats["ram_total"].endswith(" bytes)")
    assert machine._machine_stat_display_value(stats, "ram").count(" / ") == 1


def test_machine_stat_display_and_print_skip_unknowns(capsys):
    stats = {
        "os": "OS",
        "architecture": None,
        "physical_cores": None,
        "logical_cores": 8,
        "ram_total": "1 GiB",
        "ram_available": "0.5 GiB",
        "disk_total": "9",
        "disk_free": "3",
        "drive_model": None,
        "drive_speed": "SSD/non-rotational",
    }
    assert (
        machine._machine_stat_display_value(stats, "cores")
        == "unknown physical / 8 logical"
    )
    assert machine._machine_stat_display_value(stats, "drive") is None
    assert machine._machine_stat_display_value(stats, "cpu_model") is None
    machine._print_machine_stats(stats)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Machine stats:"
    assert (
        "  Disk: 9 total / 3 free" in out and "  Drive speed: SSD/non-rotational" in out
    )
    assert not any(
        line.startswith(("  Architecture", "  Drive:", "  CPU:")) for line in out
    )


def test_record_base_file_size(tmp_path):
    meta: dict = {}
    machine._record_base_file_size(meta, tmp_path / "none.bam")
    assert meta == {"base_file_size": "not available", "base_file_size_bytes": None}
    (tmp_path / "a.bam").write_bytes(b"x" * 2048)
    machine._record_base_file_size(meta, tmp_path / "a.bam")
    assert meta == {
        "base_file_size": "2.0 KiB (2,048 bytes)",
        "base_file_size_bytes": 2048,
    }


DISKUTIL = """
   Device Identifier:         disk3s1
   Device / Media Name:       APPLE SSD AP1024Z
   Protocol:                  Apple Fabric
"""
PROFILER = """Macintosh HD:

          Mount Point: /
          Medium Type: SSD
          Protocol: Apple Fabric
          Device Name: APPLE SSD AP0512Q

Data:

          Mount Point: /Volumes/Data
          Medium Type: HDD
          Protocol: USB
          Device Name: WD Elements
"""


@pytest.mark.parametrize(
    ("diskutil", "model", "speed"),
    [
        (DISKUTIL, "APPLE SSD AP1024Z", "Apple Fabric"),
        ("   Media Name: Ext Disk\n", "Ext Disk", "HDD, USB"),
        (None, "WD Elements", "HDD, USB"),
    ],
)
def test_macos_drive_model_and_speed_fall_back_to_system_profiler(
    monkeypatch, diskutil, model, speed
):
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(machine, "_filesystem_mount", lambda _p: "/Volumes/Data")
    outputs = {"diskutil": diskutil, "system_profiler": PROFILER}
    monkeypatch.setattr(machine, "_command_output", lambda cmd, **_k: outputs[cmd[0]])
    assert machine._drive_model(Path("/Volumes/Data/x")) == model
    assert machine._drive_speed(Path("/Volumes/Data/x")) == speed


@pytest.mark.parametrize(
    ("flag", "speed"),
    [("1\n", "HDD/rotational"), ("0\n", "SSD/non-rotational"), (None, None)],
)
def test_linux_drive_speed_reads_sys_block_rotational(
    tmp_path, monkeypatch, flag, speed
):
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine, "_linux_sys_block_name", lambda _p: "nvme0n1")
    queue = tmp_path / "nvme0n1" / "queue"
    queue.mkdir(parents=True)
    if flag is not None:
        (queue / "rotational").write_text(flag)
    real_path = Path
    monkeypatch.setattr(
        machine, "Path", lambda p: tmp_path if p == "/sys/block" else real_path(p)
    )
    assert machine._drive_speed(tmp_path) == speed


def test_unknown_platform_has_no_drive_info(monkeypatch):
    monkeypatch.setattr(machine.platform, "system", lambda: "Plan9")
    assert (
        machine._drive_model(Path("/")) is None
        and machine._drive_speed(Path("/")) is None
    )
    monkeypatch.setattr(machine.platform, "system", lambda: "Linux")
    monkeypatch.setattr(machine, "_linux_sys_block_name", lambda _p: None)
    assert machine._drive_speed(Path("/")) is None
