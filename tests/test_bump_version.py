"""scripts/bump_version.py changes all three versions or none."""

import importlib.util
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
FILES = ["pyproject.toml", "pixi.toml", "src/wgsextract_cli/__init__.py"]


@pytest.fixture
def bump(tmp_path, monkeypatch):
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SCRIPT.parents[1] / name, tmp_path / name)
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def test_bumps_all_three(bump, tmp_path):
    assert bump.main(["bump", "9.8.7"]) == 0
    for name in FILES:
        assert '"9.8.7"' in (tmp_path / name).read_text()


def test_a_failed_write_changes_nothing(bump, tmp_path, monkeypatch):
    before = {name: (tmp_path / name).read_text() for name in FILES}
    real_replace = Path.replace

    def fail_on_pixi(self, target):
        if Path(target).name == "pixi.toml":
            raise OSError("disk full")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_on_pixi)
    assert bump.main(["bump", "9.8.7"]) == 1
    assert {name: (tmp_path / name).read_text() for name in FILES} == before
    assert not list(tmp_path.rglob("*.bump"))
