"""scripts/bump_version.py changes every version, the app's CLI pin too, or none."""

import importlib.util
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
FILES = ["pyproject.toml", "pixi.toml", "src/wgsextract_cli/__init__.py"]
GUI_TAG = "gui/examples/WGSExtract/scripts/wgsextract-release-tag.txt"
GUI_FILES = [
    "gui/scripts/bump-wgsextract-cli.py",
    GUI_TAG,
    "gui/docs/distribution.md",
    "gui/examples/WGSExtract/manifest.json",
    "gui/app/tests/conformance.test.mjs",
    "gui/tools/packaging/tests/test_embedded_branding.py",
    "gui/docs/CHANGELOG.md",
]


@pytest.fixture
def bump(tmp_path, monkeypatch):
    for name in FILES + GUI_FILES:
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
    # Issue #510: the app ships in the same release, so it bundles this CLI.
    assert (tmp_path / GUI_TAG).read_text() == "v9.8.7\n"


def test_a_failed_app_bump_changes_nothing(bump, tmp_path):
    before = {name: (tmp_path / name).read_text() for name in FILES}
    (tmp_path / "gui/docs/distribution.md").write_text("no version here\n")
    assert bump.main(["bump", "9.8.7"]) == 1
    assert {name: (tmp_path / name).read_text() for name in FILES} == before
    assert (tmp_path / GUI_TAG).read_text() != "v9.8.7\n"


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
