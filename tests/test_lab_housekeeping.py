import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

LAB_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "installlab"
DAY = 86400


@pytest.fixture
def hk(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "housekeeping", LAB_SCRIPTS / "housekeeping.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "housekeeping", module)
    spec.loader.exec_module(module)
    lab = tmp_path / "out" / "installlab"
    monkeypatch.setattr(module, "REPO", tmp_path)
    monkeypatch.setattr(module, "LAB", lab)
    monkeypatch.setattr(module, "WIN", lab / "windows")
    monkeypatch.setattr(module, "LEDGER", lab / "last-used.json")
    # No Tart, Docker, Pixi or Homebrew: only the files under out/installlab.
    monkeypatch.setattr(module, "_tool", lambda name: None)
    monkeypatch.setattr(module, "_qemu_command_lines", lambda: "")
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    return module


def make(path: Path, age_days: float, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix:
        path.write_bytes(b"x" * size)
    else:
        path.mkdir(exist_ok=True)
        (path / "f").write_bytes(b"x" * size)
    stamp = time.time() - age_days * DAY
    for p in (path, *path.rglob("*")):
        os.utime(p, (stamp, stamp))
    return path


def test_runs_expire_and_serve_goes_after_a_day(hk):
    old = make(hk.LAB / "runs" / "linux-install-old", 20)
    recent = make(hk.LAB / "runs" / "linux-install-recent", 2)
    serve = make(recent / "serve", 2)
    fresh = make(hk.LAB / "runs" / "linux-install-fresh", 0.1)
    fresh_serve = make(fresh / "serve", 0.1)
    old_log = make(hk.LAB / "run1.log", 20)

    hk.collect_garbage(verbose=False)

    assert not old.exists() and not old_log.exists() and not serve.exists()
    assert recent.exists() and fresh_serve.exists()


def test_last_use_is_what_counts_not_age(hk):
    base = make(hk.WIN / "base.qcow2", 90)
    make(hk.WIN / "base-vars.fd", 90)
    base.chmod(0o444)
    hk.mark_used(hk.lab_key(base))

    hk.collect_garbage(verbose=False)
    assert base.exists()

    hk.LEDGER.unlink()
    hk.collect_garbage(verbose=False)
    assert not base.exists() and not (hk.WIN / "base-vars.fd").exists()


def test_base_outlives_the_gui_base_built_on_it(hk):
    base = make(hk.WIN / "base.qcow2", 90)
    gui_base = make(hk.WIN / "gui-base.qcow2", 90)
    hk.mark_used(hk.lab_key(gui_base))

    hk.collect_garbage(verbose=False)

    assert base.exists() and gui_base.exists()


def test_leftovers_of_killed_runs_go_unless_still_running(hk, monkeypatch):
    dead = make(hk.WIN / "runs" / "wgse-lab-dead0001", 0.1)
    live = make(hk.WIN / "runs" / "wgse-lab-live0001", 0.1)
    young = make(hk.WIN / "runs" / "wgse-lab-young001", 0.01)
    qmp = make(hk.tempfile.gettempdir() / Path("wgse-qmp-abc"), 0.1)
    monkeypatch.setattr(
        hk, "_qemu_command_lines", lambda: f"qemu -drive file={live}/disk.qcow2"
    )

    hk.collect_garbage(verbose=False)

    assert not dead.exists() and not qmp.exists()
    assert live.exists() and young.exists()


def test_downloads_expire_sooner_than_images(hk):
    iso = make(hk.WIN / "win11-arm64.iso", 10)
    cache = make(hk.LAB / "cache", 10)

    hk.collect_garbage(verbose=False)

    assert not iso.exists() and cache.exists()


def test_dry_run_removes_nothing(hk, capsys):
    old = make(hk.LAB / "runs" / "linux-install-old", 20, size=4096)

    freed = hk.collect_garbage(dry_run=True)

    assert old.exists() and freed > 0
    assert "would remove" in capsys.readouterr().out
