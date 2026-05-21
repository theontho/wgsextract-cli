from argparse import Namespace

from wgsextract_cli.commands import ref as ref_command
from wgsextract_cli.core import config


def test_bootstrap_saves_reference_library_when_unconfigured(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    reflib = tmp_path / "reference"

    monkeypatch.setattr(config, "get_config_path", lambda: config_path)
    config.settings.clear()
    monkeypatch.setattr(
        "wgsextract_cli.core.reference_processing.download_bootstrap", lambda path: True
    )
    monkeypatch.delenv("WGSEXTRACT_INSTALL_MAPPABILITY_MAPS", raising=False)

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
        "wgsextract_cli.core.reference_processing.download_bootstrap", lambda path: True
    )
    monkeypatch.delenv("WGSEXTRACT_INSTALL_MAPPABILITY_MAPS", raising=False)

    ref_command.cmd_bootstrap(Namespace(ref=str(requested_reflib)))

    config.reload_settings()
    assert config.settings["reference_library"] == str(existing_reflib)


def test_bootstrap_installs_mappability_maps_when_requested(tmp_path, monkeypatch):
    reflib = tmp_path / "reference"
    calls = []

    monkeypatch.setattr(
        "wgsextract_cli.core.reference_processing.download_bootstrap", lambda path: True
    )
    monkeypatch.setattr(
        "wgsextract_cli.core.ref_library.install_mappability_maps",
        lambda path: calls.append(path) or True,
    )

    ref_command.cmd_bootstrap(
        Namespace(ref=str(reflib), install_mappability_maps=True)
    )

    assert calls == [str(reflib)]


def test_bootstrap_installs_mappability_maps_from_env(tmp_path, monkeypatch):
    reflib = tmp_path / "reference"
    calls = []

    monkeypatch.setattr(
        "wgsextract_cli.core.reference_processing.download_bootstrap", lambda path: True
    )
    monkeypatch.setattr(
        "wgsextract_cli.core.ref_library.install_mappability_maps",
        lambda path: calls.append(path) or True,
    )
    monkeypatch.setenv("WGSEXTRACT_INSTALL_MAPPABILITY_MAPS", "1")

    ref_command.cmd_bootstrap(Namespace(ref=str(reflib)))

    assert calls == [str(reflib)]
