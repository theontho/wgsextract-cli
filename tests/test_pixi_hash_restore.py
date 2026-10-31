import runpy
import sys
from pathlib import Path

import pytest

from scripts.restore_pixi_hashes import restore_hashes


def test_hash_restoration_without_stdlib_tomllib(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "tomllib", None)
    helper = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "restore_pixi_hashes.py")
    )
    digest = "a" * 64
    url = f"https://example.invalid/source.tar.gz#sha256={digest}"
    manifest = tmp_path / "pixi.toml"
    manifest.write_text(f'[pypi-dependencies]\npackage = {{ url = "{url}" }}\n')
    lock = tmp_path / "pixi.lock"
    lock.write_text(f"version: 6\npackages:\n- pypi: direct+{url}\n  name: package\n")

    assert helper["restore_hashes"](manifest, lock) == 1
    assert f"  sha256: {digest}\n" in lock.read_text()


def test_restore_direct_archive_hash_without_reformatting_lock(tmp_path: Path) -> None:
    digest = "a" * 64
    url = f"https://example.invalid/source.tar.gz#sha256={digest}"
    manifest = tmp_path / "pixi.toml"
    manifest.write_text(f'[pypi-dependencies]\npackage = {{ url = "{url}" }}\n')
    lock = tmp_path / "pixi.lock"
    original = f"version: 6\npackages:\n- pypi: direct+{url}\n  name: package\n"
    lock.write_text(original)
    assert restore_hashes(manifest, lock) == 1
    assert lock.read_text() == original + f"  sha256: {digest}\n"
    assert restore_hashes(manifest, lock) == 0


def test_conflicting_lock_hash_fails_closed(tmp_path: Path) -> None:
    manifest = tmp_path / "pixi.toml"
    manifest.write_text(
        '[dependencies]\npackage = { url = "https://example.invalid/package.conda", '
        f'sha256 = "{"a" * 64}" }}\n'
    )
    lock = tmp_path / "pixi.lock"
    original = (
        "version: 6\npackages:\n- conda: https://example.invalid/package.conda\n"
        f"  sha256: {'b' * 64}\n"
    )
    lock.write_text(original)
    with pytest.raises(ValueError, match="disagrees"):
        restore_hashes(manifest, lock)
    assert lock.read_text() == original
