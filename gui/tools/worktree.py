#!/usr/bin/env python3
"""Create and remove developer Git worktrees for this repository."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


LOCAL_CONFIG_FILES = (".dev_id", ".devconfig.toml")
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    branch: str


def fail(message: str) -> int:
    print(f"worktree: error: {message}", file=sys.stderr)
    return 1


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of: 1, 0, true, false, yes, no, on, off")


def env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def run_capture(args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_checked(args: list[str], *, cwd: Path | None = None) -> None:
    printable = " ".join(args)
    print(f"worktree: $ {printable}")
    proc = subprocess.run(args, cwd=cwd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed with exit {proc.returncode}: {printable}")


def repo_root() -> Path:
    status, output = run_capture(["git", "rev-parse", "--show-toplevel"])
    if status != 0:
        raise RuntimeError("not inside a Git repository")
    return Path(output.strip()).resolve()


def sanitize_branch_name(branch: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip(".-")
    if not sanitized:
        raise RuntimeError("BRANCH must contain at least one path-safe character")
    return sanitized


def default_worktree_root(root: Path) -> Path:
    return root.parent / f"{root.name}-worktrees"


def resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def default_worktree_path(root: Path, branch: str, worktree_root: str) -> Path:
    parent = resolve_path(Path(worktree_root)) if worktree_root else default_worktree_root(root)
    return parent / sanitize_branch_name(branch)


def ensure_path_is_safe(root: Path, path: Path) -> None:
    resolved = resolve_path(path)
    if resolved == root or root in resolved.parents:
        raise RuntimeError(f"refusing to use a worktree path inside this checkout: {resolved}")
    if resolved == resolved.anchor:
        raise RuntimeError(f"refusing to use filesystem root as a worktree path: {resolved}")


def parse_worktrees(output: str) -> list[WorktreeEntry]:
    entries: list[WorktreeEntry] = []
    current_path: Path | None = None
    current_branch = ""
    for line in [*output.splitlines(), ""]:
        if line.startswith("worktree "):
            if current_path is not None:
                entries.append(WorktreeEntry(current_path, current_branch))
            current_path = Path(line[len("worktree ") :])
            current_branch = ""
        elif line.startswith("branch "):
            raw_branch = line[len("branch ") :]
            current_branch = raw_branch.removeprefix("refs/heads/")
        elif not line and current_path is not None:
            entries.append(WorktreeEntry(current_path, current_branch))
            current_path = None
            current_branch = ""
    return entries


def registered_worktrees(root: Path) -> list[WorktreeEntry]:
    status, output = run_capture(["git", "worktree", "list", "--porcelain"], cwd=root)
    if status != 0:
        raise RuntimeError(output.strip() or "failed to list worktrees")
    return parse_worktrees(output)


def find_worktree(root: Path, path: Path) -> WorktreeEntry | None:
    resolved = resolve_path(path)
    for entry in registered_worktrees(root):
        if resolve_path(entry.path) == resolved:
            return entry
    return None


def checked_out_worktree(root: Path, branch: str) -> WorktreeEntry | None:
    for entry in registered_worktrees(root):
        if entry.branch == branch:
            return entry
    return None


def ref_exists(root: Path, ref: str) -> bool:
    status, _ = run_capture(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root)
    return status == 0


def branch_exists(root: Path, branch: str) -> bool:
    status, _ = run_capture(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root)
    return status == 0


def remote_exists(root: Path, remote: str = "origin") -> bool:
    status, _ = run_capture(["git", "remote", "get-url", remote], cwd=root)
    return status == 0


def maybe_fetch(root: Path, fetch: bool) -> None:
    if fetch and remote_exists(root):
        run_checked(["git", "fetch", "--prune", "origin"], cwd=root)


def add_worktree(root: Path, path: Path, branch: str, base: str) -> None:
    existing_checkout = checked_out_worktree(root, branch)
    if existing_checkout is not None:
        raise RuntimeError(
            f"branch {branch!r} is already checked out at {resolve_path(existing_checkout.path)}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        run_checked(["git", "worktree", "add", str(path), branch], cwd=root)
        return

    remote_ref = f"origin/{branch}"
    if ref_exists(root, remote_ref):
        run_checked(["git", "worktree", "add", "--track", "-b", branch, str(path), remote_ref], cwd=root)
        return

    if not ref_exists(root, base):
        raise RuntimeError(f"base ref not found: {base}")
    run_checked(["git", "worktree", "add", "-b", branch, str(path), base], cwd=root)


def copy_local_config(root: Path, path: Path, overwrite: bool) -> None:
    for name in LOCAL_CONFIG_FILES:
        source = root / name
        destination = path / name
        if not source.exists() or (destination.exists() and not overwrite):
            continue
        shutil.copy2(source, destination)
        print(f"worktree: copied {name}")


def setup_command(path: Path) -> list[str]:
    if sys.platform == "win32":
        script = path / "make.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell.exe")
        if executable is None:
            raise RuntimeError("PowerShell was not found on PATH; run setup manually in the worktree")
        return [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "setup"]
    return ["make", "setup"]


def run_developer_setup(path: Path) -> None:
    run_checked(setup_command(path), cwd=path)


def setup(args: argparse.Namespace) -> int:
    root = repo_root()
    branch = args.branch
    if not branch:
        return fail("BRANCH is required, for example: make worktree-setup BRANCH=my-feature")

    path = resolve_path(Path(args.path)) if args.path else default_worktree_path(root, branch, args.root)
    ensure_path_is_safe(root, path)
    maybe_fetch(root, args.fetch)

    existing = find_worktree(root, path)
    if existing is None:
        if path.exists() and any(path.iterdir()):
            return fail(f"target path exists and is not a registered worktree: {path}")
        add_worktree(root, path, branch, args.base)
    elif existing.branch != branch:
        return fail(f"{path} is already registered for branch {existing.branch!r}, not {branch!r}")
    else:
        print(f"worktree: using existing worktree at {path}")

    if args.copy_local_config:
        copy_local_config(root, path, args.overwrite_local_config)
    if args.run_setup:
        run_developer_setup(path)

    print(f"worktree: ready at {path}")
    return 0


def worktree_status(path: Path) -> str:
    status, output = run_capture(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=path,
    )
    if status != 0:
        raise RuntimeError(output.strip() or f"failed to inspect worktree status: {path}")
    return output


def remove_worktree(root: Path, path: Path, force: bool) -> None:
    args = ["git", "worktree", "remove", str(path)]
    if force:
        args.insert(3, "--force")
    run_checked(args, cwd=root)


def teardown(args: argparse.Namespace) -> int:
    root = repo_root()
    branch = args.branch
    if not branch and not args.path:
        return fail("BRANCH or WORKTREE_PATH is required")

    path = resolve_path(Path(args.path)) if args.path else default_worktree_path(root, branch, args.root)
    ensure_path_is_safe(root, path)
    entry = find_worktree(root, path)
    if entry is None:
        return fail(f"not a registered worktree for this repository: {path}")
    if branch and entry.branch != branch:
        return fail(f"{path} is registered for branch {entry.branch!r}, not {branch!r}")

    dirty = worktree_status(path)
    if dirty and not args.force:
        print(dirty, end="", file=sys.stderr)
        return fail("worktree has local changes or untracked files; rerun with FORCE=1 to remove it")

    try:
        remove_worktree(root, path, args.force)
    except RuntimeError:
        if dirty or args.force:
            raise
        print("worktree: retrying removal with --force for ignored setup artifacts")
        remove_worktree(root, path, True)
    run_checked(["git", "worktree", "prune"], cwd=root)
    print(f"worktree: removed {path}")
    return 0


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="Create and initialize a developer worktree.")
    setup_parser.add_argument("--branch", default=env_value("WORKTREE_BRANCH", "BRANCH"))
    setup_parser.add_argument("--path", default=env_value("WORKTREE_PATH", "WORKTREE"))
    setup_parser.add_argument("--root", default=env_value("WORKTREE_ROOT"))
    setup_parser.add_argument("--base", default=env_value("WORKTREE_BASE", "BASE", default="origin/main"))
    setup_parser.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        default=env_bool("WORKTREE_FETCH", True),
    )
    setup_parser.add_argument(
        "--skip-setup",
        dest="run_setup",
        action="store_false",
        default=env_bool("WORKTREE_RUN_SETUP", True),
    )
    setup_parser.add_argument(
        "--no-copy-local-config",
        dest="copy_local_config",
        action="store_false",
        default=env_bool("WORKTREE_COPY_LOCAL_CONFIG", True),
    )
    setup_parser.add_argument(
        "--overwrite-local-config",
        action="store_true",
        default=env_bool("WORKTREE_OVERWRITE_LOCAL_CONFIG", False),
    )
    setup_parser.set_defaults(func=setup)

    teardown_parser = subparsers.add_parser("teardown", help="Remove a registered developer worktree.")
    teardown_parser.add_argument("--branch", default=env_value("WORKTREE_BRANCH", "BRANCH"))
    teardown_parser.add_argument("--path", default=env_value("WORKTREE_PATH", "WORKTREE"))
    teardown_parser.add_argument("--root", default=env_value("WORKTREE_ROOT"))
    teardown_parser.add_argument("--force", action="store_true", default=env_bool("FORCE", False))
    teardown_parser.set_defaults(func=teardown)
    return parser


def main() -> int:
    try:
        args = parser().parse_args()
        return args.func(args)
    except (OSError, RuntimeError, ValueError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
