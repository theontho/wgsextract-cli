"""Dev download cache items: keys, ages, pruning and failures that must only
skip the cache, never fail a download."""

import logging
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wgsextract_cli.core import dev_download_cache as cache
from wgsextract_cli.core.config import settings

URL = "https://example.test/genomes/hs38.fa.gz"


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    monkeypatch.setitem(settings, "dev_download_cache", True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    source = tmp_path / "hs38.fa.gz"
    source.write_bytes(b"reference")
    return source


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_cache_home_follows_xdg_or_defaults_to_dot_cache(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert (
        cache.download_cache_root()
        == Path.home() / ".cache" / "wgsextract" / "downloads"
    )

    monkeypatch.setenv("XDG_CACHE_HOME", "~/xdg")
    assert cache.benchmark_dataset_cache_root() == (
        Path.home() / "xdg" / "wgsextract" / "benchmark-datasets"
    )


def test_an_expired_item_is_pruned_instead_of_restored(enabled, tmp_path):
    cached = cache.store_download_in_dev_cache(URL, enabled)
    assert cached is not None
    _age(cached, cache.CACHE_TTL_SECONDS + 60)

    assert cache.restore_cached_download(URL, tmp_path / "out" / "hs38.fa.gz") is None
    assert not cached.exists()
    assert not (tmp_path / "out").exists()


def test_restoring_an_item_renews_its_age(enabled, tmp_path):
    cached = cache.store_download_in_dev_cache(URL, enabled)
    assert cached is not None
    _age(cached, cache.CACHE_TTL_SECONDS - 60)

    assert cache.restore_cached_download(URL, tmp_path / "out.fa.gz") == cached
    assert not cache.cache_item_is_expired(cached)
    assert time.time() - cached.stat().st_mtime < 60


def test_items_are_keyed_by_url_and_checksum(enabled, tmp_path):
    plain = cache.cached_download_path(URL, enabled)
    hinted = cache.cached_download_path(URL, enabled, checksum_hint="sha256:ab")
    other = cache.cached_download_path(URL + "?v=2", enabled)

    assert len({plain, hinted, other}) == 3
    assert plain.parent == cache.download_cache_root()
    assert plain.name.endswith("-hs38.fa.gz")

    cache.store_download_in_dev_cache(URL, enabled, checksum_hint="sha256:ab")
    assert cache.restore_cached_download(URL, tmp_path / "x.fa.gz") is None
    assert (
        cache.restore_cached_download(
            URL, tmp_path / "x.fa.gz", checksum_hint="sha256:ab"
        )
        == hinted
    )


@pytest.mark.parametrize(
    ("url", "destination", "name"),
    [
        ("https://h.test/a%20b/my%20ref (v2).fa.gz", "x", "my-ref-v2-.fa.gz"),
        ("https://h.test/", "fallback.bin", "fallback.bin"),
        ("https://h.test/...", "x", "download"),
    ],
)
def test_cache_file_names_are_safe(url, destination, name):
    assert cache._cache_filename(url, Path(destination)) == name


def test_storing_the_cache_item_itself_only_renews_it(enabled):
    cached = cache.store_download_in_dev_cache(URL, enabled)
    assert cached is not None
    _age(cached, 3600)

    assert cache.store_download_in_dev_cache(URL, cached) == cached
    assert cached.read_bytes() == b"reference"
    assert time.time() - cached.stat().st_mtime < 60


def test_an_unusable_cache_skips_caching(enabled, tmp_path, monkeypatch):
    (tmp_path / "xdg").write_text("a file where the cache folder should be")
    assert cache.store_download_in_dev_cache(URL, enabled) is None

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg2"))

    def disk_full(src, dst):
        Path(dst).write_bytes(b"part")
        raise OSError("No space left on device")

    monkeypatch.setattr(cache.shutil, "copyfile", disk_full)
    assert cache.store_download_in_dev_cache(URL, enabled) is None
    assert list(cache.download_cache_root().iterdir()) == []


def test_missing_sources_are_not_stored(enabled, tmp_path):
    assert cache.store_download_in_dev_cache(URL, tmp_path / "nope.fa.gz") is None


def test_drop_removes_an_item_and_tolerates_failures(enabled, caplog):
    cached = cache.store_download_in_dev_cache(URL, enabled)
    assert cached is not None

    cache.drop_cached_download(URL, enabled)
    assert not cached.exists()
    cache.drop_cached_download(URL, enabled)  # already gone

    cached.mkdir()  # something unlink can't remove
    caplog.set_level(logging.DEBUG)
    cache.drop_cached_download(URL, enabled)
    assert "Could not remove dev download cache item" in caplog.text


def test_prune_removes_stale_folders_and_keeps_fresh_ones(tmp_path):
    root = tmp_path / "cache"
    stale = root / "old-dataset"
    (stale / "sub").mkdir(parents=True)
    (stale / "sub" / "reads.fq").write_text("x")
    _age(stale, cache.CACHE_TTL_SECONDS + 60)
    fresh = root / "new-dataset"
    fresh.mkdir()

    cache.prune_expired_cache_items(root)

    assert not stale.exists() and fresh.exists()
    cache.prune_expired_cache_items(tmp_path / "missing")
    (tmp_path / "file").write_text("x")
    cache.prune_expired_cache_items(tmp_path / "file")  # not a folder: no error


def test_marking_a_folder_used_renews_it(tmp_path):
    folder = tmp_path / "dataset"
    folder.mkdir()
    _age(folder, 3600)

    cache.mark_cache_item_used(folder)

    assert (folder / ".dev-cache-touch").exists()
    assert not cache.cache_item_is_expired(folder)
    cache.mark_cache_item_used(tmp_path / "gone")  # no error
    assert cache.cache_item_is_expired(tmp_path / "gone")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, cache.CACHE_TTL_SECONDS), (3600, 3600), ("120", 120), (-5, 0)],
)
def test_ttl_setting(monkeypatch, value, expected):
    monkeypatch.setitem(settings, "dev_download_cache_ttl_seconds", value)
    assert cache._cache_ttl_seconds() == expected


def test_an_invalid_ttl_setting_warns_and_uses_the_default(monkeypatch, caplog):
    monkeypatch.setitem(settings, "dev_download_cache_ttl_seconds", "a week")

    assert cache._cache_ttl_seconds() == cache.CACHE_TTL_SECONDS
    assert "Ignoring invalid dev_download_cache_ttl_seconds='a week'" in caplog.text


# --- staged, verified restores (#277) --------------------------------------


def _checker(expected: bytes, seen: list[Path], destination: Path):
    def verify(path: Path) -> bool:
        assert not destination.exists(), "final path visible before verification"
        assert path.parent == destination.parent
        seen.append(path)
        return path.read_bytes() == expected

    return verify


def test_restore_verifies_a_staged_copy_before_publishing(enabled, tmp_path):
    cached = cache.store_download_in_dev_cache(URL, enabled, checksum_hint="h")
    destination = tmp_path / "out" / "hs38.fa.gz"
    seen: list[Path] = []

    assert (
        cache.restore_cached_download(
            URL,
            destination,
            checksum_hint="h",
            verify=_checker(b"reference", seen, destination),
        )
        == cached
    )
    assert destination.read_bytes() == b"reference"
    assert len(seen) == 1 and seen[0] != destination
    assert list(destination.parent.iterdir()) == [destination]


def test_a_corrupt_restore_is_dropped_and_never_published(enabled, tmp_path):
    cached = cache.store_download_in_dev_cache(URL, enabled, checksum_hint="h")
    assert cached is not None
    destination = tmp_path / "out" / "hs38.fa.gz"

    assert (
        cache.restore_cached_download(
            URL,
            destination,
            checksum_hint="h",
            verify=_checker(b"expected bytes", [], destination),
        )
        is None
    )
    assert not cached.exists()
    assert list(destination.parent.iterdir()) == []


def test_an_interrupted_restore_leaves_no_final_or_staged_file(enabled, tmp_path):
    cached = cache.store_download_in_dev_cache(URL, enabled, checksum_hint="h")
    assert cached is not None
    destination = tmp_path / "out" / "hs38.fa.gz"

    def interrupted(path: Path) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cache.restore_cached_download(
            URL, destination, checksum_hint="h", verify=interrupted
        )
    assert list(destination.parent.iterdir()) == []
    assert cached.read_bytes() == b"reference"


def test_concurrent_restores_stage_separately_and_publish_once_verified(
    enabled, tmp_path
):
    cached = cache.store_download_in_dev_cache(URL, enabled, checksum_hint="h")
    destination = tmp_path / "out" / "hs38.fa.gz"
    both_staged = threading.Barrier(2, timeout=10)
    seen: list[Path] = []
    check = _checker(b"reference", seen, destination)

    def verify(path: Path) -> bool:
        ok = check(path)
        both_staged.wait()
        return ok

    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: cache.restore_cached_download(
                    URL, destination, checksum_hint="h", verify=verify
                ),
                range(2),
            )
        )

    assert results == [cached, cached]
    assert len(set(seen)) == 2
    assert destination.read_bytes() == b"reference"
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize(
    ("published", "restored"), [(b"reference", True), (b"referen", False)]
)
def test_a_windows_sharing_violation_accepts_only_an_identical_concurrent_publish(
    enabled, tmp_path, monkeypatch, published, restored
):
    """Windows cannot replace a file another run holds open for reading."""
    cached = cache.store_download_in_dev_cache(URL, enabled, checksum_hint="h")
    destination = tmp_path / "out" / "hs38.fa.gz"
    destination.parent.mkdir()
    real_replace = os.replace

    def replace(src, dst):
        if Path(dst) == destination:
            destination.write_bytes(published)
            raise PermissionError(13, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(cache.os, "replace", replace)
    monkeypatch.setattr(cache.time, "sleep", lambda _: None)

    result = cache.restore_cached_download(URL, destination, checksum_hint="h")
    assert result == (cached if restored else None)
    assert [p.name for p in destination.parent.iterdir()] == [destination.name]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_restored_and_cached_files_get_the_fresh_download_mode(enabled, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.write_bytes(b"x")
    expected = stat.S_IMODE(fresh.stat().st_mode)
    cached = cache.store_download_in_dev_cache(URL, enabled)
    assert cached is not None
    assert stat.S_IMODE(cached.stat().st_mode) == expected
    cached.chmod(0o600)  # an entry stored before this fix
    destination = tmp_path / "out" / "hs38.fa.gz"

    assert cache.restore_cached_download(URL, destination) == cached
    assert stat.S_IMODE(destination.stat().st_mode) == expected
