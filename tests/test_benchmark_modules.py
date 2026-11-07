"""Benchmark building blocks: dataset download/cache/checksum/extract, CLI step
execution and reporting, fixtures, and machine stats. Nothing touches the
network; the release dataset zip is rebuilt locally in its published layout
(see scripts/build_benchmark_dataset.py)."""

import gzip
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from tests.fake_tools import python_tool, require_bare_name_fakes
from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    needs_tools,
    sam_to_bam,
    write_fasta,
)
from wgsextract_cli.api import CancelToken, Result
from wgsextract_cli.api.benchmark import BenchmarkParams, benchmark
from wgsextract_cli.commands import benchmark as benchmark_entry
from wgsextract_cli.commands.benchmark import (
    datasets,
    environment,
    execution,
    fixtures,
    machine,
)
from wgsextract_cli.commands.benchmark.models import (
    BenchmarkDatasetSpec,
    BenchmarkDerivedAlignment,
    BenchmarkJob,
    BenchmarkRemoteFile,
)
from wgsextract_cli.core import dev_download_cache
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.ref_library import downloads
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.main import main

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
    def getcode(self) -> int:
        return 200

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

    monkeypatch.setattr(downloads, "urlopen", urlopen)
    monkeypatch.setattr(downloads, "curl_progress_args", lambda: ["--silent"])
    monkeypatch.setattr(downloads.time, "sleep", lambda _: None)
    return files, fetched


# --- datasets: download, cache, checksum ----------------------------------


def test_download_file_writes_atomically_and_retains_partial_for_resume(
    tmp_path, served, monkeypatch
):
    files, _ = served
    files["https://example.test/a.zip"] = b"payload"
    dest = tmp_path / "a.zip"
    datasets._download_file("https://example.test/a.zip", dest)
    assert dest.read_bytes() == b"payload"
    assert not (tmp_path / "a.zip.tmp").exists()

    original_copy = downloads.copy_response_to_file

    def broken(response, handle, **_kwargs):
        handle.write(b"half")
        raise OSError("connection reset")

    monkeypatch.setattr(downloads, "copy_response_to_file", broken)
    failed = tmp_path / "b.zip"
    with pytest.raises(WGSExtractError, match="b.zip"):
        datasets._download_file("https://example.test/a.zip", failed)
    assert not failed.exists()
    assert (tmp_path / "b.zip.partial").read_bytes() == b"half"
    monkeypatch.setattr(downloads, "copy_response_to_file", original_copy)
    datasets._download_file("https://example.test/a.zip", failed)
    assert failed.read_bytes() == b"payload"
    assert not (tmp_path / "b.zip.partial").exists()


def test_cached_remote_file_downloads_verifies_and_then_trusts_marker(tmp_path, served):
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

    # The marker vouches for an unchanged file without rehashing it: same-size
    # bytes swapped in under the old mtime are kept, not refetched.
    stat = path.stat()
    path.write_bytes(b"CRAM BYTES")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert datasets._cached_remote_dataset_file(remote, tmp_path) == path
    assert path.read_bytes() == b"CRAM BYTES"
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

    # An older marker that records only the MD5 can't vouch for the file.
    (tmp_path / "sample.cram").write_bytes(b"CRAM BYTES")
    datasets._verified_checksum_path(tmp_path / "sample.cram", remote.md5).write_text(
        remote.md5 + "\n"
    )
    assert datasets._cached_remote_dataset_file(remote, tmp_path).read_bytes() == good
    assert fetched == [url, url, url, url]


def test_direct_real_dataset_downloads_into_the_shared_cache_root(
    tmp_path, served, monkeypatch
):
    files, _ = served
    monkeypatch.setitem(settings, "dev_download_cache", False)
    files["https://example.test/ref.fa"] = b">chr1\nACGT\n"
    files["https://example.test/sample.bam"] = b"bam"
    spec = BenchmarkDatasetSpec(
        tag="real-test",
        dataset_id="real-test",
        description="test direct dataset",
        build="hg38",
        sample="HG00096",
        kind="direct",
        remote_files=(
            BenchmarkRemoteFile("ref", "https://example.test/ref.fa", "ref.fa"),
            BenchmarkRemoteFile("bam", "https://example.test/sample.bam", "sample.bam"),
        ),
        region_safe=True,
    )
    run_dataset = tmp_path / "run-dataset"

    dataset = datasets._prepare_direct_real_benchmark_dataset(
        BenchmarkJob(), run_dataset, tmp_path, spec
    )

    root = tmp_path / "datasets" / "real-test"
    assert dataset.root == root
    assert (root / "sample.bam").read_bytes() == b"bam"
    # The run only notes where the shared files are; it holds no copies.
    assert (run_dataset / "real-test-cache-root.txt").exists()
    assert not (run_dataset / "real-test" / "sample.bam").exists()


def test_cached_remote_file_drops_corrupt_dev_cache_copy_and_downloads(
    tmp_path, served, monkeypatch
):
    files, fetched = served
    url = "https://example.test/sample.cram"
    files[url] = b"good"
    remote = BenchmarkRemoteFile(
        "bam", url, "sample.cram", hashlib.md5(b"good").hexdigest()
    )
    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    stale = tmp_path / "stale.cram"
    stale.write_bytes(b"stale")
    hint = f"md5:{remote.md5}"
    cached = dev_download_cache.store_download_in_dev_cache(
        url, stale, checksum_hint=hint
    )
    assert cached is not None
    cache_root = tmp_path / "datasets"

    assert (
        datasets._cached_remote_dataset_file(remote, cache_root).read_bytes() == b"good"
    )
    assert fetched == [url]
    assert cached.read_bytes() == b"good"
    assert sorted(p.name for p in cache_root.iterdir()) == [
        f".sample.cram.{remote.md5}.verified",
        "sample.cram",
    ]


def test_real_dataset_cache_dir_precedence(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setitem(settings, "dev_download_cache", False)
    assert datasets._real_dataset_cache_dir(BenchmarkJob(), out) == out / "datasets"
    explicit = BenchmarkJob(dataset_cache_dir=tmp_path / "x" / ".." / "cache")
    assert datasets._real_dataset_cache_dir(explicit, out) == tmp_path / "cache"
    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert (
        datasets._real_dataset_cache_dir(BenchmarkJob(), out)
        == tmp_path / "xdg" / "wgsextract" / "benchmark-datasets"
    )


def test_dataset_sha256_and_filename_normalisation():
    def sha(value):
        return datasets._normalized_dataset_sha256(BenchmarkJob(dataset_sha256=value))

    assert (sha(" AB "), sha("  "), sha(None)) == ("ab", None, None)
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


def test_md5_pinned_github_asset_does_not_require_release_sha_metadata(
    tmp_path, served, monkeypatch
):
    monkeypatch.setitem(settings, "dev_download_cache", False)
    files, fetched = served
    url = "https://github.com/example/benchmarks/releases/download/test/sample.cram"
    metadata = "https://api.github.com/repos/example/benchmarks/releases/tags/test"
    payload = b"MD5-pinned benchmark data"
    files[url] = payload
    files[metadata] = json.dumps({"assets": [{"name": "sample.cram"}]}).encode()
    remote = BenchmarkRemoteFile(
        "bam", url, "sample.cram", hashlib.md5(payload).hexdigest()
    )
    assert (
        datasets._cached_remote_dataset_file(remote, tmp_path).read_bytes() == payload
    )
    assert fetched == [url]


def test_real_archive_downloads_once_verifies_and_extracts(
    tmp_path, served, monkeypatch
):
    monkeypatch.setitem(settings, "dev_download_cache", False)
    files, fetched = served
    asset = _dataset_zip(tmp_path / "asset.zip")
    url = "https://example.test/rel/wgsextract-benchmark-hg19-mini.zip?x=1"
    files[url] = asset.read_bytes()
    args = BenchmarkJob(
        dataset="real", dataset_url=url, dataset_sha256=_sha256(asset).upper()
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
    args = BenchmarkJob(
        dataset_url=url, dataset_sha256="", dataset_cache_dir=tmp_path / "c"
    )
    first = environment._real_dataset_zip_path(args, tmp_path)
    assert (
        environment._real_dataset_zip_path(args, tmp_path)
        == first
        == tmp_path / "c" / "d.zip"
    )
    assert fetched == [url]


@pytest.mark.parametrize("checksum", ["", "  ", None])
def test_empty_archive_checksum_skips_missing_github_release_digest(
    tmp_path, served, monkeypatch, checksum
):
    monkeypatch.setitem(settings, "dev_download_cache", False)
    files, fetched = served
    url = "https://github.com/example/benchmarks/releases/download/test/fixture.zip"
    metadata = "https://api.github.com/repos/example/benchmarks/releases/tags/test"
    files[url] = _dataset_zip(tmp_path / "asset.zip").read_bytes()
    files[metadata] = json.dumps({"assets": [{"name": "fixture.zip"}]}).encode()
    args = BenchmarkJob(
        dataset_url=url, dataset_sha256=checksum, dataset_cache_dir=tmp_path / "c"
    )
    if checksum is None:
        with pytest.raises(WGSExtractError, match="Failed to download"):
            environment._real_dataset_zip_path(args, tmp_path)
        assert fetched == [metadata]
    else:
        downloaded = environment._real_dataset_zip_path(args, tmp_path)
        assert downloaded.read_bytes() == files[url]
        assert fetched == [url]


def test_real_archive_restores_from_dev_cache_without_downloading(
    tmp_path, served, monkeypatch
):
    files, fetched = served
    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    url = "https://example.test/d.zip"
    files[url] = _dataset_zip(tmp_path / "a.zip").read_bytes()
    args = BenchmarkJob(
        dataset_url=url,
        dataset_sha256=_sha256(tmp_path / "a.zip"),
        dataset_cache_dir=tmp_path / "c",
    )
    zip_path = environment._real_dataset_zip_path(args, tmp_path)
    zip_path.unlink()
    assert environment._real_dataset_zip_path(args, tmp_path).read_bytes() == files[url]
    assert fetched == [url]


def test_local_dataset_zip_is_checked_and_missing_one_rejected(tmp_path):
    asset = _dataset_zip(tmp_path / "local.zip")
    args = BenchmarkJob(dataset="real", dataset_zip=asset, dataset_sha256=None)
    dataset = environment._prepare_real_benchmark_dataset(
        args, tmp_path / "run", tmp_path
    )
    assert dataset.ref.name == DATASET_FILES["ref"]
    args = replace(args, dataset_zip=tmp_path / "nope.zip")
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
def test_derived_alignment_subsamples_to_indexed_cram_and_is_reused(tmp_path):
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

    built = {p: p.stat() for p in (tmp_path / "sub.cram", tmp_path / "sub.crai")}
    datasets._prepare_derived_alignment(
        spec, {"source_cram": source, "ref": ref}, tmp_path
    )
    for path, stat in built.items():
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == (
            stat.st_ino,
            stat.st_mtime_ns,
        )
    with pytest.raises(WGSExtractError, match="requires source alignment and ref"):
        datasets._prepare_derived_alignment(spec, {"ref": ref}, tmp_path)


# --- execution -------------------------------------------------------------


def _fake_python(monkeypatch, bindir: Path, code: str) -> None:
    """Benchmark steps run the CLI with ``sys.executable``; this one runs ``code``."""
    monkeypatch.setattr(sys, "executable", str(python_tool(bindir, "python", code)))


@pytest.mark.parametrize("keep_awake", [None, True, False])
def test_cli_step_child_inherits_config_flags_threads_memory_and_source(
    monkeypatch, tmp_path, keep_awake
):
    from wgsextract_cli.core import config

    own = tmp_path / "own.toml"
    own.write_text("")
    config.set_config_path(own)
    monkeypatch.setitem(config.settings, "keep_awake", True)
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    log = tmp_path / "child.json"
    _fake_python(monkeypatch, tmp_path / "bin", f"""
import json, os, sys
json.dump({{"argv": sys.argv[1:], "pythonpath": os.environ["PYTHONPATH"]}},
          open({str(log)!r}, "w"))
""")  # fmt: skip
    args = BenchmarkJob(
        debug=True, quiet=True, memory="2G", threads=3, keep_awake=keep_awake
    )
    result = execution._run_cli_step(
        args, "Sort", "05-sort", ["bam", "sort"], tmp_path, tmp_path / "logs", []
    )

    assert result.status == "PASS"
    child = json.loads(log.read_text())
    argv = child["argv"]
    assert argv[:4] == ["-m", "wgsextract_cli.main", "--parent-pid", str(os.getpid())]
    # contract: each step runs with the parent's config file, its sleep override,
    # its log level, the planned thread count and memory.
    assert argv[4:] == [
        "--config",
        str(own.resolve()),
        *({True: ["--keep-awake"], False: ["--no-keep-awake"]}.get(keep_awake, [])),
        "--debug",
        "bam",
        "sort",
        "--threads",
        "3",
        "--memory",
        "2G",
    ]
    assert result.command == [sys.executable, *argv]
    # The child imports this source tree, not another installed copy.
    src = str(Path(benchmark_entry.__file__).resolve().parents[2])
    assert child["pythonpath"].split(os.pathsep) == [src, "/elsewhere"]

    quiet = execution._run_cli_step(
        BenchmarkJob(quiet=True), "Info", "i", ["info"], tmp_path,
        tmp_path / "logs", [],
    )  # fmt: skip
    assert json.loads(log.read_text())["argv"][-2:] == ["--quiet", "info"]
    assert quiet.status == "PASS"


def test_cli_step_passes_logs_output_and_label(tmp_path, monkeypatch):
    out = tmp_path / "step" / "o.txt"
    _fake_python(
        monkeypatch,
        tmp_path / "bin",
        f"import sys; open({str(out)!r}, 'w').write('x'); print('hi'); print('warn', file=sys.stderr)",
    )
    result = execution._run_cli_step(
        BenchmarkJob(),
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
    _fake_python(monkeypatch, tmp_path / "bin", code)
    result = execution._run_cli_step(
        BenchmarkJob(), "N", "s", [], tmp_path, tmp_path, [tmp_path / "gone"]
    )
    assert result.status == "FAIL" and result.error.startswith(error)
    assert result.name == "N"


def test_pipe_step_streams_file_through_the_command(tmp_path, monkeypatch):
    _fake_python(
        monkeypatch,
        tmp_path / "bin",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())",
    )
    src, dst = tmp_path / "in.txt", tmp_path / "out" / "o.txt"
    src.write_bytes(b"acgt\n")
    dst.parent.mkdir()
    result = fixtures._run_cli_pipe_step(
        BenchmarkJob(),
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
        BenchmarkJob(),
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


# --- benchmark(): failure handling and report ------------------------------


def _run(tmp_path: Path, **overrides) -> Result:
    values = {
        "profile": "smoke",
        "seed": 1,
        "suite": "core",
        "outdir": tmp_path,
        "threads": 2,
        "dataset_sha256": None,
    }
    values.update(overrides)
    return benchmark(BenchmarkParams(**values), check=False)


_STEP_PYTHON = """\
import subprocess, sys

argv = sys.argv[1:]
if argv[:2] == ["-m", "wgsextract_cli.main"]:
    command = " ".join(argv)
    for broken in {fail!r}:
        if f" {{broken}} " in f"{{command}} ":
            sys.exit(f"{{broken}} went wrong")
    if {noop!r}:
        sys.exit(0)
sys.exit(subprocess.call([sys.executable, *argv]))
"""


def _steps(monkeypatch, tmp_path: Path, *fail: str, noop: bool = False) -> None:
    """Benchmark steps fail when they run a listed command; the others run the
    real CLI, or with ``noop`` do nothing."""
    _fake_python(
        monkeypatch, tmp_path / "bin", _STEP_PYTHON.format(fail=fail, noop=noop)
    )


def _report(tmp_path: Path) -> dict:
    (path,) = tmp_path.glob("runs/*/benchmark_results.json")
    return json.loads(path.read_text())


needs_benchmark_tools = needs_tools("samtools", "bcftools", "bgzip", "tabix", "bwa")


@needs_benchmark_tools
def test_failed_step_stops_run_and_still_writes_report(tmp_path, monkeypatch):
    _steps(monkeypatch, tmp_path, "info --detailed")
    result = _run(tmp_path)
    assert result.exit_code == 1
    assert re.search(
        r"BAM metadata .*00a-info-detailed\.stderr\.log", result.error.message
    )
    out = result.text
    assert "info --detailed went wrong" in out
    slugs = [r["slug"] for r in _report(tmp_path)["results"]]
    assert slugs == ["00-generate-bam", "00a-info-detailed"]
    assert "Failed: 1" in out


@needs_benchmark_tools
def test_internal_step_failure_message_shows_its_error(tmp_path):
    # A region beyond the end of chrM leaves no contig to place targets on.
    error = _run(tmp_path, region="chrM:17000-18000").error.message
    assert "No reference contigs match benchmark region" in error
    assert "See None" not in error
    assert _report(tmp_path)["results"][-1]["slug"] == "02-microarray-targets"


@needs_benchmark_tools
def test_keep_going_runs_remaining_steps_and_reports_failures_at_the_end(
    tmp_path, monkeypatch
):
    _steps(monkeypatch, tmp_path, "extract mt-bam", "microarray")
    result = _run(tmp_path, keep_going=True)
    assert result.error.message == (
        "2 benchmark step(s) failed: Mitochondrial BAM extraction [extract mt-bam]; "
        "Microarray CombinedKit generation [microarray]"
    )
    status = {r["slug"]: r["status"] for r in _report(tmp_path)["results"]}
    assert [slug for slug, s in status.items() if s == "FAIL"] == [
        "04b-mt-bam",
        "10-microarray",
    ]
    # The steps after each failure still ran, and passed.
    assert status["04c-ydna-bam"] == status["11-vcf-qc"] == "PASS"
    assert list(status.values()).count("PASS") == len(status) - 2


def test_real_smoke_run_uses_archive_files_and_records_base_size(tmp_path, monkeypatch):
    _steps(monkeypatch, tmp_path, noop=True)
    asset = _dataset_zip(
        tmp_path / "asset.zip", {"default_region": "chrM:1-500", "region_safe": True}
    )
    result = _run(
        tmp_path / "o",
        dataset="real",
        dataset_zip=asset,
        dataset_sha256=_sha256(asset),
        keep_going=True,
    )
    assert "benchmark step" in result.error.message

    report = _report(tmp_path / "o")
    meta, results = report["metadata"], report["results"]
    steps = {r["slug"]: r for r in results}
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
    # The archive's own targets are used, not generated ones.
    assert steps["00-load-real-dataset"]["status"] == "PASS"
    assert steps["02-microarray-targets"]["status"] == "PASS"
    assert steps["02-microarray-targets"]["expected_outputs"] == [
        str(root / DATASET_FILES["targets"]),
        str(root / DATASET_FILES["targets_index"]),
    ]
    assert str(root / DATASET_FILES["bam"]) in steps["00a-info-detailed"]["command"]
    assert steps["03-bam-unalign"]["command"][-4:-2] == ["--region", "MT:1-500"]
    assert "00-generate-bam" not in steps
    md = (Path(meta["run_dir"]) / "benchmark_report.md").read_text()
    assert "- Data source: `real` (HG00096 mini)" in md
    assert (
        f"Benchmark base file: {root / DATASET_FILES['bam']} ({len(DATASET_FILES['bam'])} bytes)"
        in result.text
    )


def test_unsafe_real_dataset_ignores_region(tmp_path, monkeypatch):
    _steps(monkeypatch, tmp_path, noop=True)
    asset = _dataset_zip(tmp_path / "asset.zip")
    result = _run(
        tmp_path / "o", dataset="real", dataset_zip=asset, region="MT", keep_going=True
    )
    assert "benchmark step" in result.error.message
    report = _report(tmp_path / "o")
    steps = {r["slug"]: r for r in report["results"]}
    assert report["metadata"]["region"] == "MT"
    assert "--region" not in steps["03-bam-unalign"]["command"]
    assert "--region" not in steps["04a-bam-subset"]["command"]


@pytest.mark.parametrize(("flag", "value"), [("coverage", 0.0), ("target_count", -1)])
def test_run_rejects_non_positive_sizes(tmp_path, flag, value):
    result = _run(tmp_path, **{flag: value})
    assert result.exit_code == 2
    assert "must be greater than zero" in result.error.message
    assert not (tmp_path / "runs").exists()


@needs_benchmark_tools
def test_cancelling_a_running_step_stops_its_child_and_removes_the_run(
    tmp_path, monkeypatch
):
    """#478: a cancel mid-step kills the step's CLI child and deletes the run."""
    marker = tmp_path / "child.pid"
    _fake_python(monkeypatch, tmp_path / "bin", f"""
import os, sys, time
if sys.argv[1:3] != ["-m", "wgsextract_cli.main"]:
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
open({str(marker)!r}, "w").write(str(os.getpid()))
time.sleep(60)
""")  # fmt: skip
    token = CancelToken()

    def cancel_once_the_child_runs() -> None:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        token.cancel()

    watcher = threading.Thread(target=cancel_once_the_child_runs)
    watcher.start()
    started = time.monotonic()
    result = benchmark(
        BenchmarkParams(outdir=tmp_path / "o", profile="smoke", suite="core"),
        cancel=token,
        check=False,
    )
    watcher.join()

    assert marker.exists(), "the step's child never started"
    assert (result.status, result.exit_code) == ("cancelled", 130)
    assert time.monotonic() - started < 30
    assert not psutil.pid_exists(int(marker.read_text()))
    assert list((tmp_path / "o" / "runs").iterdir()) == []


@needs_benchmark_tools
@pytest.mark.parametrize("outdir", [None, "~/bench"])
def test_cli_run_ignores_the_configured_output_directory(tmp_path, monkeypatch, outdir):
    """The CLI's runs go to out/benchmark (or -o, with ~ expanded), never to
    the config's output_directory."""
    configured = tmp_path / "configured"
    own = tmp_path / "own.toml"
    own.write_text(f"output_directory = {str(configured)!r}\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    _steps(monkeypatch, tmp_path, noop=True)
    argv = ["--config", str(own), "benchmark", "--profile", "smoke"]
    argv += ["--suite", "core", "-t", "2", "--dataset-sha256", ""]
    with pytest.raises(SystemExit):
        main([*argv, *(["-o", outdir] if outdir else [])])

    root = tmp_path / ("home/bench" if outdir else "out/benchmark")
    assert len(list(root.glob("runs/*/benchmark_results.json"))) == 1
    assert not list(configured.rglob("*.json"))


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
    tmp_path, monkeypatch, diskutil, model, speed
):
    require_bare_name_fakes()
    bindir = tmp_path / "bin"
    # diskutil knows only the volume's mount point, and may say nothing useful.
    python_tool(bindir, "diskutil", f"""
import sys
if sys.argv[1:] != ["info", "/Volumes/Data"] or {diskutil!r} is None:
    sys.exit(1)
print({diskutil!r})
""")  # fmt: skip
    python_tool(bindir, "system_profiler", f"print({PROFILER!r})")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(machine.platform, "system", lambda: "Darwin")
    volumes = [SimpleNamespace(mountpoint=m) for m in ("/", "/Volumes/Data")]
    monkeypatch.setattr(psutil, "disk_partitions", lambda all=False: volumes)

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
