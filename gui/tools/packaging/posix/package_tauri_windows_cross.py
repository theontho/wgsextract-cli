"""Build the Windows x64 Tauri NSIS installer on macOS or Linux.

The same installer `package_tauri.ps1` builds on Windows, cross-compiled with
cargo-xwin (Microsoft's CRT and SDK, fetched by cargo-xwin) and NSIS's makensis,
as Tauri documents for non-Windows hosts. The bundled Node runtime is the
Windows x64 one. The installer is not Authenticode-signed (signing needs a
Windows host or a configured sign command).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from common import REPO_ROOT, repo, reset_dir

TARGET = "x86_64-pc-windows-msvc"
TAURI_DIR = Path("app/web/packagers/tauri")
REQUIRED_TOOLS = {
    "cargo-xwin": "cargo install --locked cargo-xwin",
    "makensis": "brew install makensis (macOS) or apt install nsis (Linux)",
    "lld-link": "brew install lld (macOS) or apt install lld (Linux)",
    "clang-cl": "brew install llvm (macOS, then add $(brew --prefix llvm)/bin to PATH) or apt install clang",
    "llvm-rc": "brew install llvm (macOS) or apt install llvm",
}


def _add_tool_dirs_to_path() -> None:
    """Where these tools install without landing on PATH: cargo's bin, and
    Homebrew's keg-only llvm and lld."""
    candidates = [Path.home() / ".cargo" / "bin"]
    brew = shutil.which("brew")
    if brew:
        for formula in ("llvm", "lld"):
            prefix = subprocess.run(
                [brew, "--prefix", formula], capture_output=True, text=True, check=False
            ).stdout.strip()
            if prefix:
                candidates.append(Path(prefix) / "bin")
    path = os.environ.get("PATH", "").split(os.pathsep)
    extra = [str(d) for d in candidates if d.is_dir() and str(d) not in path]
    if extra:
        os.environ["PATH"] = os.pathsep.join([*path, *extra])


def check_prerequisites() -> None:
    _add_tool_dirs_to_path()
    missing = [f"  {tool}: {how}" for tool, how in REQUIRED_TOOLS.items() if not shutil.which(tool)]
    if not (REPO_ROOT / "app/node_modules/.bin/tsc").exists():
        missing.append("  Node packages: npm ci --prefix app")
    targets = subprocess.run(
        ["rustup", "target", "list", "--installed"], capture_output=True, text=True, check=False
    ).stdout.split()
    if TARGET not in targets:
        missing.append(f"  Rust target {TARGET}: rustup target add {TARGET}")
    if missing:
        raise SystemExit("Cross-building the Windows installer needs:\n" + "\n".join(missing))


def stage_tauri_windows_cross_release(release_dir: Path) -> Path:
    if sys.platform not in ("darwin", "linux"):
        raise SystemExit("tauri-windows-cross runs on macOS or Linux; on Windows use `package tauri`.")
    check_prerequisites()
    typescript = REPO_ROOT / "app"
    subprocess.run(["npm", "run", "build"], cwd=typescript, check=True)
    subprocess.run(
        ["node", "scripts/prepare-tauri-node.mjs", "--target", "win32-x64"], cwd=typescript, check=True
    )
    bundle_dir = repo(TAURI_DIR) / "target" / TARGET / "release/bundle/nsis"
    shutil.rmtree(bundle_dir, ignore_errors=True)
    # Windows builds carry no distribution suffix in their product name (see run-tauri.mjs).
    env = {**os.environ, "TAURI_PRODUCT_SUFFIX": "none"}
    subprocess.run(
        ["node", "scripts/run-tauri.mjs", "build", "--runner", "cargo-xwin", "--target", TARGET,
         "--bundles", "nsis"],
        cwd=typescript, env=env, check=True,
    )  # fmt: skip
    installers = sorted(bundle_dir.glob("*-setup.exe"))
    if len(installers) != 1:
        raise SystemExit(f"Expected one NSIS installer, found {[p.name for p in installers]}")
    dest = reset_dir(release_dir)
    shutil.copy2(installers[0], dest / installers[0].name)
    print(dest / installers[0].name)
    return dest / installers[0].name
