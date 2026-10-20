import logging

from wgsextract_cli.commands.ref import library_commands
from wgsextract_cli.core import reference_resolver
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.reference_resolver import ReferenceLibrary


def test_a_missing_configured_library_is_reported_once(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "WGSExtract" / "reference"
    monkeypatch.setitem(settings, "reference_library", str(missing))
    reference_resolver._warn_missing_configured_library.cache_clear()

    with caplog.at_level(logging.WARNING):
        ReferenceLibrary(None)
        ReferenceLibrary(None)
        ReferenceLibrary(str(tmp_path))  # an explicit --ref isn't the config's

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(missing) in warnings[0] and "doesn't exist" in warnings[0]
    assert not missing.exists()


def test_downloading_into_a_new_library_says_so(tmp_path, caplog):
    new = tmp_path / "reference"
    with caplog.at_level(logging.INFO):
        library_commands._run_downloader(str(new), "ClinVar", lambda reflib: True)
    assert f"Creating a new reference library at {new}." in caplog.text
