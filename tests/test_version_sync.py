from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import wgsextract_cli

ROOT = Path(__file__).resolve().parents[1]


def test_project_version_declarations_are_aligned() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pixi = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == pixi["workspace"]["version"]
    assert wgsextract_cli.__version__ == pyproject["project"]["version"]
