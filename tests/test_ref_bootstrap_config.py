from argparse import Namespace

import pytest

from wgsextract_cli.commands import ref as ref_command
from wgsextract_cli.core import config, reference_processing
from wgsextract_cli.core.utils import WGSExtractError


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

    ref_command.cmd_bootstrap(Namespace(ref=str(reflib), install_mappability_maps=True))

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


def test_bootstrap_saves_reference_library_before_map_install_failure(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    reflib = tmp_path / "reference"

    monkeypatch.setattr(config, "get_config_path", lambda: config_path)
    config.settings.clear()
    monkeypatch.setattr(
        "wgsextract_cli.core.reference_processing.download_bootstrap", lambda path: True
    )
    monkeypatch.setattr(
        "wgsextract_cli.core.ref_library.install_mappability_maps", lambda path: False
    )

    with pytest.raises(WGSExtractError):
        ref_command.cmd_bootstrap(
            Namespace(ref=str(reflib), install_mappability_maps=True)
        )

    config.reload_settings()
    assert config.settings["reference_library"] == str(reflib)


def test_download_bootstrap_normalizes_existing_assets_and_installs_ploidy(
    tmp_path, monkeypatch
):
    reflib = tmp_path / "reference"
    nested_ref = reflib / "reference" / "ref"
    nested_ref.mkdir(parents=True)
    (nested_ref / "All_SNPs_hg19_ref.tab.gz").write_text("snps\n", encoding="utf-8")
    (nested_ref / ".DS_Store").write_text("metadata\n", encoding="utf-8")

    monkeypatch.setattr(
        reference_processing,
        "run_command",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": "* * * M 2\n"})(),
    )

    assert reference_processing.download_bootstrap(str(reflib))
    assert (reflib / "ref" / "All_SNPs_hg19_ref.tab.gz").is_file()
    assert not (reflib / "ref" / ".DS_Store").exists()
    assert (reflib / "ploidy_hg19.txt").is_file()
    assert (reflib / "ploidy_hg38.txt").is_file()
