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


@pytest.mark.parametrize("runtime", ["swiftui", "tauri"])
def test_no_window_mutation_removes_the_active_runtime_evidence(oracle, runtime):
    import gui_oracle
    import lab

    (oracle.results / "lab-run.json").write_text(
        json.dumps({"target": "host", "macos_runtime": runtime})
    )
    (oracle.results / "startup.txt").write_text("window_appeared_ms=42\n")
    (oracle.results / "app-stdout.txt").write_text("metric webAppRendered_ms=42\n")
    lab.GUI_MUTATIONS["no-window"](oracle.results)
    current = gui_oracle.GuiOracle(oracle.results, "nonce", {}, signed=True)
    current.check_app_reports()
    window = next(
        check for check in current.checks if check.name == "app.window_appeared"
    )
    assert not window.ok


@pytest.mark.parametrize("runtime", ["swiftui", "tauri"])
def test_gatekeeper_off_mutation_is_checked_by_both_macos_runtimes(oracle, runtime):
    import gui_oracle
    import lab
    import macos_policy

    (oracle.results / "lab-run.json").write_text(
        json.dumps({"target": "host", "macos_runtime": runtime})
    )
    (oracle.results / "gatekeeper-status.txt").write_text("assessments enabled\n")
    (oracle.results / "gatekeeper-policy.json").write_text(
        json.dumps(dict.fromkeys(macos_policy.POLICY_KEYS, True))
    )
    current = gui_oracle.GuiOracle(oracle.results, "nonce", {}, signed=True)
    checks = {check.name: check.ok for check in current.run(oracle.results, [])}
    assert checks["baseline.gatekeeper_enabled"]
    assert checks["baseline.gatekeeper_policy"]

    lab.GUI_MUTATIONS["gatekeeper-off"](oracle.results)
    current = gui_oracle.GuiOracle(oracle.results, "nonce", {}, signed=True)
    checks = {check.name: check.ok for check in current.run(oracle.results, [])}
    assert not checks["baseline.gatekeeper_enabled"]


def test_tauri_preferences_mutation_uses_the_current_identifier(oracle):
    import gui_oracle
    import lab

    (oracle.results / "lab-run.json").write_text(
        json.dumps(
            {"target": "host", "host_platform": "darwin", "macos_runtime": "tauri"}
        )
    )
    (oracle.results / "inventory-after_uninstall.txt").write_text("")
    lab.GUI_MUTATIONS["preferences-kept"](oracle.results)
    current = gui_oracle.GuiOracle(oracle.results, "nonce", {}, signed=True)
    added = (oracle.results / "inventory-after_uninstall.txt").read_text().splitlines()
    assert added[0].endswith(current.app.emptied_preferences)
    assert current.excuse_leftovers(added) == (added, [])


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
