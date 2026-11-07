from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
from tools.packaging.git_filters import copy_git_filtered


def repo(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def run(command: list[str], *, cwd: str | Path | None = None) -> None:
    subprocess.run(command, cwd=repo(cwd) if cwd else REPO_ROOT, check=True)


def run_tool(tool: str, args: list[str], *, cwd: str | Path | None = None) -> None:
    env = os.environ.copy()
    parts = shlex.split(tool)
    while parts and "=" in parts[0] and not parts[0].startswith("="):
        key, value = parts.pop(0).split("=", 1)
        env[key] = value
    if not parts:
        raise ValueError(f"Tool command is empty: {tool!r}")
    subprocess.run(
        [*parts, *args], cwd=repo(cwd) if cwd else REPO_ROOT, env=env, check=True
    )


def reset_dir(path: str | Path) -> Path:
    path = repo(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy_path(src: str | Path, dest: str | Path, *, git_filtered: bool = True) -> None:
    src_path = repo(src)
    dest_path = repo(dest)
    if git_filtered and src_path.is_dir():
        if copy_git_filtered(src_path, dest_path, REPO_ROOT):
            return
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ditto"):
        if dest_path.exists() and dest_path.is_dir():
            shutil.rmtree(dest_path)
        run(["ditto", str(src_path), str(dest_path)])
        return
    if src_path.is_dir():
        if dest_path.exists():
            shutil.rmtree(dest_path)
        shutil.copytree(src_path, dest_path, symlinks=True)
    else:
        shutil.copy2(src_path, dest_path)


def make_executable(path: str | Path) -> None:
    path = repo(path)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
