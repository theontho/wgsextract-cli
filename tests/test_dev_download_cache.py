import os
import time
from argparse import Namespace

from wgsextract_cli.commands._benchmark_datasets import _real_dataset_cache_dir
from wgsextract_cli.core import dev_download_cache, ref_library


def test_dev_download_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WGSEXTRACT_DEV_DOWNLOAD_CACHE", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    source = tmp_path / "source.fa.gz"
    source.write_bytes(b"cached reference")
    destination = tmp_path / "fresh" / "source.fa.gz"
    url = "https://example.test/source.fa.gz"

    cached = dev_download_cache.store_download_in_dev_cache(
        url, source, checksum_hint="sha256:test"
    )

    assert cached is not None
    assert destination.exists() is False
    assert (
        dev_download_cache.restore_cached_download(
            url, destination, checksum_hint="sha256:test"
        )
        == cached
    )
    assert destination.read_bytes() == b"cached reference"


def test_dev_download_cache_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("WGSEXTRACT_DEV_DOWNLOAD_CACHE", "0")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    source = tmp_path / "source.fa.gz"
    source.write_bytes(b"cached reference")
    url = "https://example.test/source.fa.gz"

    assert dev_download_cache.store_download_in_dev_cache(url, source) is None
    assert (
        dev_download_cache.restore_cached_download(url, tmp_path / "dest.fa.gz") is None
    )


def test_dev_download_cache_defaults_to_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("WGSEXTRACT_DEV_DOWNLOAD_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    source = tmp_path / "source.fa.gz"
    source.write_bytes(b"cached reference")

    assert (
        dev_download_cache.store_download_in_dev_cache(
            "https://example.test/source.fa.gz", source
        )
        is None
    )


def test_prune_expired_cache_items_removes_stale_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WGSEXTRACT_DEV_DOWNLOAD_CACHE_TTL_SECONDS", "1")
    root = tmp_path / "cache"
    root.mkdir()
    stale = root / "old.zip"
    fresh = root / "fresh.zip"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    old = time.time() - 10
    os.utime(stale, (old, old))

    dev_download_cache.prune_expired_cache_items(root)

    assert not stale.exists()
    assert fresh.exists()


def test_ref_library_download_file_uses_dev_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("WGSEXTRACT_DEV_DOWNLOAD_CACHE", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setattr(
        ref_library, "resolve_github_release_asset_sha256", lambda url: None
    )

    def fail_run_command(*_args, **_kwargs):
        raise AssertionError(
            "curl should not run when the dev cache satisfies download"
        )

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError(
            "urlopen should not run when the dev cache satisfies download"
        )

    monkeypatch.setattr(ref_library, "run_command", fail_run_command)
    monkeypatch.setattr(ref_library, "urlopen", fail_urlopen)

    source = tmp_path / "source.fa.gz"
    source.write_bytes(b"cached reference")
    url = "https://example.test/source.fa.gz"
    dev_download_cache.store_download_in_dev_cache(url, source)

    destination = tmp_path / "downloads" / "source.fa.gz"
    assert ref_library.download_file(url, str(destination))
    assert destination.read_bytes() == b"cached reference"


def test_real_benchmark_dataset_cache_defaults_to_xdg_for_devs(tmp_path, monkeypatch):
    monkeypatch.setenv("WGSEXTRACT_DEV_DOWNLOAD_CACHE", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    args = Namespace(dataset_cache_dir=None)

    assert _real_dataset_cache_dir(args, tmp_path / "out") == (
        tmp_path / "xdg-cache" / "wgsextract" / "benchmark-datasets"
    )
