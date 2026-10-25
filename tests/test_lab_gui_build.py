import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

LAB_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "installlab"


@pytest.fixture
def dmg_lab(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(LAB_SCRIPTS))
    monkeypatch.delitem(sys.modules, "gui_build", raising=False)
    import gui_build

    gui = tmp_path / "gui"
    gui.mkdir()
    dmg = tmp_path / "test.dmg"
    dmg.write_bytes(b"DMG fixture")
    serve = tmp_path / "serve"
    serve.mkdir()
    mount = tmp_path / "mounted"
    mount.mkdir()
    resources = mount / "WGSExtract.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["hdiutil", "detach"]:
            shutil.rmtree(mount / "WGSExtract.app")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gui_build, "run", run)
    monkeypatch.setattr(gui_build.tempfile, "mkdtemp", lambda **kwargs: str(mount))
    monkeypatch.setattr(
        gui_build.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "", "Authority=Developer ID Application: Test"
        ),
    )
    return gui_build, gui, dmg, serve, resources, commands


def test_tauri_dmg_uses_its_packaged_runner_without_swift(dmg_lab):
    build, gui, dmg, serve, resources, commands = dmg_lab
    node = resources / "node/bin/node"
    node.parent.mkdir(parents=True)
    node.touch()
    runner = resources / "app/dist/web/src/server/bundle-test-cli.js"
    runner.parent.mkdir(parents=True)
    runner.touch()

    assert build.build_gui_artifacts(gui, dmg, serve) == (True, "tauri")
    assert (serve / "WGSExtract.dmg").read_bytes() == dmg.read_bytes()
    assert not (serve / "gui-runner.tar.gz").exists()
    assert all(command[0] != "swift" for command in commands)


def test_incomplete_tauri_dmg_fails_and_is_detached(dmg_lab):
    build, gui, dmg, serve, resources, commands = dmg_lab
    node = resources / "node/bin/node"
    node.parent.mkdir(parents=True)
    node.touch()

    with pytest.raises(FileNotFoundError, match="bundle-test runner"):
        build.build_gui_artifacts(gui, dmg, serve)
    assert commands[-1][:2] == ["hdiutil", "detach"]


def test_released_swiftui_dmg_keeps_its_matching_external_runner(dmg_lab):
    build, gui, dmg, serve, resources, commands = dmg_lab
    release = gui / "platform/apple/.build/release"
    release.mkdir(parents=True)
    for name in ("gui-for-cli", "gui-for-cli_GUIForCLICore.bundle"):
        (release / name).touch()

    assert build.build_gui_artifacts(gui, dmg, serve) == (True, "swiftui")
    assert ["swift", "build", "-c", "release", "--product", "gui-for-cli"] in commands
    with tarfile.open(serve / "gui-runner.tar.gz") as archive:
        assert archive.getnames() == ["gui-for-cli", "gui-for-cli_GUIForCLICore.bundle"]
