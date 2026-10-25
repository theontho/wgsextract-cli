import json
import plistlib
import sys
from pathlib import Path

import pytest

LAB_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "installlab"
PLIST = "/Users/runner/Library/Preferences/dev.guiforcli.embed.wgsextract.plist"
OTHER = "dir\t/Users/runner/Library/Application Support/dev.guiforcli.embed.wgsextract"


@pytest.fixture
def oracle(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(LAB_SCRIPTS))
    for name in ("oracle", "gui_oracle"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import gui_oracle

    (tmp_path / "lab-run.json").write_text(json.dumps({"target": "host"}))
    return gui_oracle.GuiOracle(tmp_path, "nonce", {}, signed=True)


def save(oracle, prefs) -> None:
    data = plistlib.dumps(prefs, fmt=plistlib.FMT_BINARY)
    (oracle.results / "uninstall-preferences.plist").write_bytes(data)


def test_an_empty_preferences_file_is_excused(oracle):
    save(oracle, {})
    added, excused = oracle.excuse_leftovers([f"file\t{PLIST}", OTHER])
    assert added == [OTHER]
    assert excused == [f"excused, empty (cfprefsd rewrote it): {PLIST}"]


def test_preferences_with_any_key_are_left_behind(oracle):
    save(oracle, {"NSWindow Frame main": "x"})
    assert oracle.excuse_leftovers([f"file\t{PLIST}"]) == ([f"file\t{PLIST}"], [])


def test_preferences_without_a_saved_copy_are_left_behind(oracle):
    assert oracle.excuse_leftovers([f"file\t{PLIST}"]) == ([f"file\t{PLIST}"], [])


def test_a_corrupt_copy_is_not_empty(oracle):
    (oracle.results / "uninstall-preferences.plist").write_bytes(b"not a plist")
    assert oracle.excuse_leftovers([f"file\t{PLIST}"]) == ([f"file\t{PLIST}"], [])


def test_other_platforms_excuse_nothing(oracle):
    import gui_oracle

    save(oracle, {})
    oracle.app = gui_oracle.WINDOWS
    assert oracle.excuse_leftovers([f"file\t{PLIST}"]) == ([f"file\t{PLIST}"], [])


@pytest.mark.parametrize("rendered", [True, False])
def test_tauri_macos_requires_the_current_apps_window_evidence(oracle, rendered):
    import gui_oracle

    (oracle.results / "lab-run.json").write_text(
        json.dumps({"target": "host", "macos_runtime": "tauri"})
    )
    # The old Swift window marker must not make a Tauri run pass.
    (oracle.results / "startup.txt").write_text("window_appeared_ms=42\n")
    (oracle.results / "app-stdout.txt").write_text(
        "metric webAppRendered_ms=42\n" if rendered else "metric windowShown_ms=42\n"
    )
    tauri = gui_oracle.GuiOracle(oracle.results, "nonce", {}, signed=True)
    tauri.check_app_reports()
    window = next(
        check for check in tauri.checks if check.name == "app.window_appeared"
    )
    assert window.ok is rendered
    assert "runner" not in tauri.app.steps
    assert "/dev.guiforcli.web.embed.wgsextract/" in tauri.install_marker


@pytest.mark.parametrize(
    ("meta", "home"),
    [
        ({"target": "linux", "host_platform": "darwin"}, "/home/tester"),
        ({"target": "macos", "host_platform": "darwin"}, "/Users/admin"),
        ({"target": "host", "host_platform": "darwin", "home": "/Users/x"}, "/Users/x"),
        ({"target": "host", "host_platform": "linux", "home": "/home/x"}, "/home/x"),
    ],
)
def test_mutations_plant_the_run_machines_paths(tmp_path, monkeypatch, meta, home):
    # Where lab.py ran says nothing about a container's or VM's paths.
    monkeypatch.syspath_prepend(str(LAB_SCRIPTS))
    import lab

    (tmp_path / "lab-run.json").write_text(json.dumps(meta))
    assert lab._planted(tmp_path)["home"] == home
