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


def _linux_run(results: Path, timeline: list[str] | None, signal_text: str) -> None:
    (results / "lab-run.json").write_text(json.dumps({"target": "linux"}))
    events = [
        {"type": "step-start", "step": {"id": "install-wgsextract"}},
        {"type": "output", "id": "install-wgsextract", "text": "Installing Pixi...\n"},
        {
            "type": "step-complete",
            "result": {
                "id": "install-wgsextract",
                "status": "failed",
                "exitCode": 1,
                "signal": None,
                "stderr": signal_text,
            },
        },
        {"type": "complete", "result": {"status": "failed", "results": []}},
    ]
    (results / "setup-events.ndjson").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )
    if timeline is not None:
        (results / "timeline.tsv").write_text("".join(f"{t}\n" for t in timeline))


def _linux_checks(results: Path) -> dict[str, str]:
    import gui_oracle

    current = gui_oracle.GuiOracle(results, "nonce", {}, signed=False)
    current.check_app_reports()
    return {c.name: ("ok: " if c.ok else "FAIL: ") + c.detail for c in current.checks}


# Issue #448: the harness missed the just-launched app, ran the later steps
# beside its setup, and the app's Uninstall TERMed install.sh ("Terminated").
RACED = [
    "1000\tstep-start setup",
    "1010\tapp-launched 42",
    "1030\tsetup-events 0",
    "1040\tstep-end setup 1",
    "1050\tstep-start optional_tools",
    "1060\tstep-end optional_tools 1",
    "1070\tstep-start actions",
    "3000\tstep-end actions 1",
    "3010\tstep-start uninstall",
]


def test_a_setup_step_terminated_by_a_step_run_beside_it_names_that_step(oracle):
    _linux_run(oracle.results, RACED, "Terminated\n")
    checks = _linux_checks(oracle.results)
    assert checks["app.setup"].startswith("FAIL")
    assert "install-wgsextract got SIGTERM" in checks["app.setup"]
    # The scenario had stopped watching setup, so every step since is a suspect.
    assert "ran while setup was unwatched" in checks["app.setup"]
    assert (
        "scenario step 'uninstall', started while setup was running, whose app Uninstall stops"
        in checks["app.setup"]
    )
    assert checks["app.setup_alone"] == (
        "FAIL: the app was never seen to exit; steps started while the app ran "
        "setup: optional_tools, actions, uninstall"
    )


def test_a_setup_step_terminated_when_the_harness_closed_the_app_blames_it(oracle):
    _linux_run(
        oracle.results,
        [
            "1000\tstep-start setup",
            "1010\tapp-launched 42",
            "5000\tsetup-events 2",
            "9000\tharness-closes-app setup did not finish within 3 hours",
            "9500\tsetup-events 4",
            "9600\tapp-exited 0",
            "9700\tstep-end setup 1",
            "9800\tstep-start optional_tools",
        ],
        "Terminated\n",
    )
    checks = _linux_checks(oracle.results)
    assert "the lab harness (harness-closes-app" in checks["app.setup"]
    assert checks["app.setup_alone"].startswith("ok: the app ran setup alone")


def test_without_a_timeline_the_terminator_is_unknown_and_setup_alone_fails(oracle):
    _linux_run(oracle.results, None, "Killed\n")
    checks = _linux_checks(oracle.results)
    assert (
        "install-wgsextract got SIGKILL, from an unknown process" in checks["app.setup"]
    )
    assert checks["app.setup_alone"] == "FAIL: no timeline.tsv"


def test_an_ordinary_setup_failure_blames_no_one(oracle):
    _linux_run(oracle.results, RACED, "pixi: not found\n")
    assert "got SIG" not in _linux_checks(oracle.results)["app.setup"]


def test_the_step_beside_setup_mutation_fails_a_clean_timeline(oracle):
    import lab

    _linux_run(
        oracle.results,
        # setup's own start in the same millisecond as the launch is not beside it
        ["1000\tstep-start setup", "1000\tapp-launched 42", "9000\tapp-exited 0"],
        "",
    )
    assert _linux_checks(oracle.results)["app.setup_alone"].startswith("ok")
    lab.LINUX_GUI_MUTATIONS["step-beside-setup"](oracle.results)
    assert _linux_checks(oracle.results)["app.setup_alone"] == (
        "FAIL: steps started while the app ran setup: uninstall"
    )
