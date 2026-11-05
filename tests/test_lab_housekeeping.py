import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.fake_tools import python_tool

LAB_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "installlab"
DAY = 86400
GIT = shutil.which("git")
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the lab runs `ps` by name"
)


@pytest.fixture
def hk(tmp_path, monkeypatch):
    """housekeeping.py as run from a checkout in tmp_path, so its lab is that
    checkout's out/installlab, on a host with none of the tools it manages
    (Tart, Docker, Pixi, Homebrew) and no running QEMU: only `bin` is on PATH,
    and its `ps` lists ``bin/ps.txt``."""
    script = tmp_path / "repo" / "scripts" / "installlab" / "housekeeping.py"
    script.parent.mkdir(parents=True)
    shutil.copy(LAB_SCRIPTS / "housekeeping.py", script)
    if GIT:  # its own repository, even where tmp_path is inside another
        subprocess.run([GIT, "init", "-q", str(script.parents[2])], check=True)
    spec = importlib.util.spec_from_file_location("housekeeping", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "housekeeping", module)
    spec.loader.exec_module(module)
    assert module.LAB == tmp_path / "repo" / "out" / "installlab"
    module.WIN = module.WIN_DIRS[0]  # the ARM64 images, which most tests use
    bindir = tmp_path / "bin"
    python_tool(bindir, "ps", f"print(open({str(bindir / 'ps.txt')!r}).read())")
    (bindir / "ps.txt").write_text("")
    module.ps_lines = bindir / "ps.txt"
    monkeypatch.setenv("PATH", str(bindir))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
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


@posix_only
def test_leftovers_of_killed_runs_go_unless_still_running(hk):
    dead = make(hk.WIN / "runs" / "wgse-lab-dead0001", 0.1)
    live = make(hk.WIN / "runs" / "wgse-lab-live0001", 0.1)
    young = make(hk.WIN / "runs" / "wgse-lab-young001", 0.01)
    qmp = make(Path(tempfile.gettempdir()) / "wgse-qmp-abc", 0.1)
    live_qmp = make(Path(tempfile.gettempdir()) / "wgse-qmp-live", 0.1)
    hk.ps_lines.write_text(
        f"qemu -drive file={live}/disk.qcow2 -qmp unix:{live_qmp},server\n"
    )

    hk.collect_garbage(verbose=False)

    assert not dead.exists() and not qmp.exists()
    assert live.exists() and young.exists() and live_qmp.exists()


def test_downloads_expire_sooner_than_images(hk):
    iso = make(hk.WIN / "win11-arm64.iso", 10)
    cache = make(hk.LAB / "cache", 10)

    hk.collect_garbage(verbose=False)

    assert not iso.exists() and cache.exists()


def test_x64_windows_images_are_collected_too(hk):
    x64 = hk.LAB / "windows-x64"
    iso = make(x64 / "win11-amd64.iso", 10)
    base = make(x64 / "base.qcow2", 90)
    make(x64 / "base-vars.fd", 90)
    fresh = make(x64 / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe", 1)

    hk.collect_garbage(verbose=False)

    assert not iso.exists() and not base.exists() and fresh.exists()


def test_dry_run_removes_nothing(hk, capsys):
    old = make(hk.LAB / "runs" / "linux-install-old", 20, size=4096)

    freed = hk.collect_garbage(dry_run=True)

    assert old.exists() and freed > 0
    assert "would remove" in capsys.readouterr().out


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_worktrees_share_the_main_checkouts_lab(hk, tmp_path, monkeypatch):
    # A linked worktree's out/ goes with the worktree; base images must not.
    monkeypatch.setenv("PATH", os.path.dirname(GIT))
    main = tmp_path / "main"
    git = [GIT, "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", str(main)], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"],
                   check=True)  # fmt: skip
    linked = tmp_path / "linked"
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", str(linked)],
                   check=True)  # fmt: skip

    assert hk._main_checkout(linked).resolve() == main.resolve()
    assert hk._main_checkout(main).resolve() == main.resolve()
    assert hk._main_checkout(tmp_path / "not-a-repo") == tmp_path / "not-a-repo"
