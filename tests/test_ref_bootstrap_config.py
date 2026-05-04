from argparse import Namespace

from wgsextract_cli.commands import ref as ref_command
from wgsextract_cli.core import config


def test_bootstrap_saves_reference_library_when_unconfigured(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    reflib = tmp_path / "reference"

    monkeypatch.setattr(config, "get_config_path", lambda: config_path)
    config.settings.clear()
    monkeypatch.setattr(
        "wgsextract_cli.core.ref_library.download_bootstrap", lambda path: True
    )

    ref_command.cmd_bootstrap(Namespace(ref=str(reflib)))

    config.reload_settings()
    assert config.settings["reference_library"] == str(reflib)


def test_bootstrap_keeps_existing_reference_library(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    existing_reflib = tmp_path / "existing-reference"
    requested_reflib = tmp_path / "requested-reference"

    monkeypatch.setattr(config, "get_config_path", lambda: config_path)
    config.settings.clear()
    config.save_config({"reference_library": str(existing_reflib)})
    monkeypatch.setattr(
        "wgsextract_cli.core.ref_library.download_bootstrap", lambda path: True
    )

    ref_command.cmd_bootstrap(Namespace(ref=str(requested_reflib)))

    config.reload_settings()
    assert config.settings["reference_library"] == str(existing_reflib)
