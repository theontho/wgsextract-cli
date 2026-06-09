#!/usr/bin/env python3
"""Build the wheel, install it into a temporary venv, and smoke-test the CLI."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "out" / "package-smoke"
DIST_DIR = OUT_DIR / "dist"
VENV_DIR = OUT_DIR / "venv"


def _run(command: list[str], *, quiet: bool = False) -> None:
    print("+", " ".join(command))
    subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def _venv_python() -> Path:
    candidates = (
        VENV_DIR / "Scripts" / "python.exe",
        VENV_DIR / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No Python executable found in {VENV_DIR}")


def _wgsextract_launcher() -> Path:
    candidates = (
        VENV_DIR / "Scripts" / "wgsextract.exe",
        VENV_DIR / "Scripts" / "wgsextract.cmd",
        VENV_DIR / "bin" / "wgsextract",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No wgsextract launcher found in {VENV_DIR}")


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("uv is required for package-smoke but was not found on PATH", file=sys.stderr)
        return 1

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    _run([uv, "build", "--wheel", "--out-dir", str(DIST_DIR)])

    wheels = sorted(DIST_DIR.glob("*.whl"))
    if not wheels:
        print(f"No wheel was built in {DIST_DIR}", file=sys.stderr)
        return 1

    _run([uv, "venv", str(VENV_DIR), "--clear"])
    _run([uv, "pip", "install", "--python", str(_venv_python()), str(wheels[-1])])
    _run([str(_wgsextract_launcher()), "--help"], quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
