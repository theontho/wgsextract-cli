"""Expire what the install lab leaves on disk, so it never fills the drive.

Kept as caches while they're used, removed once they're not:

  leftovers   machines and work dirs of runs that were killed      1 hour old
  runs        runs/* evidence and loose logs                       --runs-days (14)
              (a run's serve/ dir, rebuilt for every run, goes after 1 day)
  images      the clean base images and caches runs reuse          --images-days (30)
  downloads   what only `prepare` needs to rebuild a base image    --downloads-days (7)
  packages    Pixi's and Homebrew's package caches (--package-caches only)

"Used" is when a run last cloned or booted it: Tart records that for its VMs;
for the rest the lab records it in last-used.json (mark_used), falling back to
the file's modification time. Only things the lab names and creates are
touched: wgse-lab-* VMs, containers and volumes, and the files under LAB, the
main checkout's out/installlab, which every worktree shares. `lab.py run` and `lab.py prepare` collect garbage first;
`lab.py gc --dry-run` shows what would go.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _main_checkout(repo: Path) -> Path:
    """The main checkout of repo's git repository (repo itself unless it's a
    linked worktree)."""
    try:
        common = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return repo
    return Path(common).parent if Path(common).name == ".git" else repo


# Shared by every worktree: base images take hours to build, and a worktree's
# out/ goes with the worktree (merging its branch can remove it).
LAB = _main_checkout(REPO) / "out" / "installlab"
# The Windows VMs' images: ARM64 (Apple Silicon hosts) and x64 (x86_64 Linux).
WIN_DIRS = (LAB / "windows", LAB / "windows-x64")
LEDGER = LAB / "last-used.json"
DAY = 86400.0
LEFTOVER_AGE = 3600.0
RUN_CLONE = re.compile(r"^wgse-lab-[0-9a-f]{8}$")
MACOS_BASE = "wgse-lab-macos-base"
GUI_BUILD_VOLUMES = ("wgse-lab-gui-cargo", "wgse-lab-gui-rustup", "wgse-lab-gui-target")
LINUX_IMAGE = "ubuntu:24.04"


def mark_used(key: str) -> None:
    """Record that a run used a cached thing now (keys are paths under
    out/installlab, or docker:<name>)."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ledger = _ledger()
    ledger[key] = time.time()
    tmp = LEDGER.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True))
    os.replace(tmp, LEDGER)


def lab_key(path: Path) -> str:
    return path.relative_to(LAB).as_posix() if path.is_relative_to(LAB) else str(path)


def _ledger() -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(LEDGER.read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


@dataclass
class Item:
    group: str
    what: str
    last_used: float
    max_age: float
    size: int
    remove: Callable[[], object]

    def expired(self, now: float) -> bool:
        return now - self.last_used > self.max_age


def _disk_usage(stat: os.stat_result) -> int:
    # Allocated blocks where the OS reports them (not Windows): sparse VM disks.
    blocks = getattr(stat, "st_blocks", None)
    return blocks * 512 if blocks is not None else stat.st_size


def _size(path: Path) -> int:
    if path.is_symlink() or not path.exists():
        return 0
    if path.is_file():
        return _disk_usage(path.stat())
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += _disk_usage(os.lstat(os.path.join(root, name)))
    return total


def _rm(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        # Base images are read-only (0444) and run dirs may hold read-only files.
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(root, name), 0o755)
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o644)
        path.unlink(missing_ok=True)


def _command(*cmd: str) -> Callable[[], object]:
    return functools.partial(subprocess.run, list(cmd), capture_output=True)


def _rm_all(paths: list[Path]) -> None:
    for path in paths:
        _rm(path)


def _file_item(group: str, path: Path, max_age: float, *also: Path) -> Item:
    paths = [p for p in (path, *also) if p.exists()]
    used = _ledger().get(lab_key(path)) or max(p.stat().st_mtime for p in paths)
    return Item(
        group,
        str(path.relative_to(LAB.parent.parent))
        if path.is_relative_to(LAB)
        else str(path),
        used,
        max_age,
        sum(_size(p) for p in paths),
        functools.partial(_rm_all, paths),
    )


def _tool(name: str) -> str | None:
    if name == "docker":
        return shutil.which("docker") or next(
            (p for p in ["/Applications/OrbStack.app/Contents/MacOS/xbin/docker"]
             if os.path.exists(p)),
            None,
        )  # fmt: skip
    return shutil.which(name)


def _out(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def _tart_items(images: float) -> list[Item]:
    tart = _tool("tart")
    if not tart:
        return []
    items: list[Item] = []
    for vm in json.loads(_out([tart, "list", "--format", "json"]) or "[]"):
        name = vm.get("Name", "")
        if vm.get("Source") != "local" or vm.get("Running"):
            continue
        accessed = datetime.fromisoformat(vm["Accessed"].replace("Z", "+00:00"))
        size = int(vm.get("Size", 0)) * 1024**3
        remove = _command(tart, "delete", name)
        if RUN_CLONE.match(name):
            items.append(Item("leftovers", f"tart VM {name}", accessed.timestamp(),
                              LEFTOVER_AGE, size, remove))  # fmt: skip
        elif name == MACOS_BASE:
            items.append(Item("images", f"tart VM {name}", accessed.timestamp(),
                              images, size, remove))  # fmt: skip
    return items


def _docker_items(images: float) -> list[Item]:
    docker = _tool("docker")
    if not docker or not _out([docker, "info", "--format", "{{.ID}}"]):
        return []
    items: list[Item] = []
    now = time.time()
    for line in _out([docker, "ps", "-a", "--filter", "name=^wgse-lab-",
                      "--format", "{{.Names}}\t{{.State}}"]).splitlines():  # fmt: skip
        name, state = line.split("\t")
        if state in ("exited", "dead"):
            items.append(Item("leftovers", f"container {name}", 0, LEFTOVER_AGE, 0,
                              _command(docker, "rm", "-f", name)))  # fmt: skip
    ledger = _ledger()
    # Only what a run recorded using: the image may be someone else's too.
    if f"docker:{LINUX_IMAGE}" in ledger:
        items.append(Item("images", f"image {LINUX_IMAGE}", ledger[f"docker:{LINUX_IMAGE}"],
                          images, 0, _command(docker, "rmi", LINUX_IMAGE)))  # fmt: skip
    existing = set(_out([docker, "volume", "ls", "--format", "{{.Name}}"]).split())
    volumes = [v for v in GUI_BUILD_VOLUMES if v in existing]
    if volumes:
        created = _out(
            [docker, "volume", "inspect", "-f", "{{.CreatedAt}}", volumes[0]]
        )
        fallback = (
            datetime.fromisoformat(created.split()[0]).timestamp() if created else now
        )
        items.append(Item("images", "volumes " + ", ".join(volumes),
                          ledger.get("docker:gui-build", fallback), images, 0,
                          _command(docker, "volume", "rm", *volumes)))  # fmt: skip
    return items


def _qemu_command_lines() -> str:
    return _out(["ps", "-axww", "-o", "command="])


def _lab_file_items(runs: float, images: float, downloads: float) -> list[Item]:
    items: list[Item] = []
    for run in sorted((LAB / "runs").glob("*")):
        items.append(_file_item("runs", run, runs))
        serve = run / "serve"
        if serve.is_dir():
            items.append(_file_item("runs", serve, DAY))
    for log in LAB.glob("*.log"):
        items.append(_file_item("runs", log, runs))
    qemu = _qemu_command_lines()
    for qmp in Path(tempfile.gettempdir()).glob("wgse-qmp-*"):
        if str(qmp) not in qemu:
            items.append(_file_item("leftovers", qmp, LEFTOVER_AGE))
    if (LAB / "cache").exists():
        items.append(_file_item("images", LAB / "cache", images))
    for win in WIN_DIRS:
        items += _windows_items(win, qemu, runs, images, downloads)
    return items


def _windows_items(
    win: Path, qemu: str, runs: float, images: float, downloads: float
) -> list[Item]:
    items = [_file_item("runs", log, runs) for log in win.glob("*.log")]
    for workdir in (win / "runs").glob("*"):
        if str(workdir) not in qemu:
            items.append(_file_item("leftovers", workdir, LEFTOVER_AGE))
    # The GUI base is an overlay of the base, so the base outlives it.
    gui_base = win / "gui-base.qcow2"
    if gui_base.exists():
        items.append(_file_item("images", gui_base, images, win / "gui-base-vars.fd"))
    base = win / "base.qcow2"
    if base.exists():
        item = _file_item("images", base, images, win / "base-vars.fd")
        if gui_base.exists():
            item.last_used = max(item.last_used, items[-1].last_used)
        items.append(item)
    for pattern in ("win11-*.iso", "virtio-win-*.iso",
                    "MicrosoftEdgeWebView2RuntimeInstaller*.exe"):  # fmt: skip
        for download in win.glob(pattern):
            items.append(_file_item("downloads", download, downloads))
    for nupkg in win.glob("microsoft.web.webview2.*.nupkg"):
        items.append(_file_item("images", nupkg, images))
    return items


def _package_items(days: float) -> list[Item]:
    items: list[Item] = []
    pixi = _tool("pixi")
    if pixi:
        info = json.loads(_out([pixi, "info", "--json"]) or "{}")
        cache = Path(info.get("cache_dir") or "") / "pkgs"
        if info.get("cache_dir") and cache.is_dir():
            for pkg in cache.iterdir():
                if pkg.is_dir():
                    items.append(Item("packages", f"pixi cache {pkg.name}",
                                      pkg.stat().st_mtime, days, _size(pkg),
                                      functools.partial(_rm, pkg)))  # fmt: skip
    brew = _tool("brew")
    if brew:
        items.append(Item("packages", f"Homebrew downloads older than {days / DAY:.0f} days",
                          0, 0, 0, _command(brew, "cleanup", "-s",
                                            f"--prune={days / DAY:.0f}")))  # fmt: skip
    return items


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1000
    return f"{value:.1f} GB"


def collect_garbage(
    runs_days: float = 14,
    images_days: float = 30,
    downloads_days: float = 7,
    package_days: float | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> int:
    """Remove every expired item; returns the bytes freed (or that would be)."""
    now = time.time()
    items = [
        *_tart_items(images_days * DAY),
        *_docker_items(images_days * DAY),
        *_lab_file_items(runs_days * DAY, images_days * DAY, downloads_days * DAY),
    ]
    if package_days is not None:
        items += _package_items(package_days * DAY)
    freed = 0
    for item in items:
        expired = item.expired(now)
        if expired:
            freed += item.size
            if not dry_run:
                item.remove()
        if verbose and (expired or dry_run):
            age = (now - item.last_used) / DAY if item.last_used else float("inf")
            verb = ("would remove" if dry_run else "removed") if expired else "keep"
            print(f"  {verb:12} {item.group:9} {item.what}  "
                  f"({_human(item.size)}, unused {age:.1f} d)")  # fmt: skip
    # Tart's downloaded images only seed `prepare macos`; Tart tracks their use.
    if (tart := _tool("tart")) and not dry_run:
        subprocess.run([tart, "prune", "--entries", "caches",
                        "--older-than", str(int(downloads_days))], capture_output=True)  # fmt: skip
    elif tart and verbose:
        print(
            f"  would run    tart prune --entries caches --older-than {int(downloads_days)}"
        )
    if verbose:
        print(f"{'Would free' if dry_run else 'Freed'} {_human(freed)}.")
    return freed


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true",
                        help="List every item and what would be removed.")  # fmt: skip
    parser.add_argument("--runs-days", type=float, default=14,
                        help="Keep run evidence and logs this long (default 14).")  # fmt: skip
    parser.add_argument("--images-days", type=float, default=30,
                        help="Keep base images and caches this long after their "
                             "last use (default 30).")  # fmt: skip
    parser.add_argument("--downloads-days", type=float, default=7,
                        help="Keep downloads only `prepare` needs this long (default 7).")  # fmt: skip
    parser.add_argument("--package-caches", type=float, metavar="DAYS",
                        help="Also expire Pixi and Homebrew package cache entries "
                             "older than DAYS.")  # fmt: skip


def from_arguments(args: argparse.Namespace) -> int:
    return collect_garbage(
        runs_days=args.runs_days,
        images_days=args.images_days,
        downloads_days=args.downloads_days,
        package_days=args.package_caches,
        dry_run=args.dry_run,
    )
