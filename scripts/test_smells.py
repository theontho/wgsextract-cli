#!/usr/bin/env python3
"""Flag change-detector patterns in test lines added since a base revision.

Advisory only: always exits 0. See "Write tests" in docs/developer_setup.md.

  TS001  monkeypatching a private project internal (a ``_name``)
  TS002  asserting a whole tool argv list

Mark an argv assert whose flags are the scientific contract with a
``# contract: <why>`` comment to silence TS002.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
TOOLS = {
    "bcftools",
    "bgzip",
    "bwa",
    "bwa-mem2",
    "docker",
    "gatk",
    "java",
    "minimap2",
    "pbrun",
    "pixi",
    "podman",
    "run_deepvariant",
    "samtools",
    "tabix",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def added_lines(base: str, root: Path) -> dict[Path, set[int]]:
    merge_base = _git("merge-base", base, "HEAD", cwd=root).strip()
    diff = _git(
        "diff", "-U0", "--diff-filter=AMR", merge_base, "--", "tests/*.py", cwd=root
    )
    added: dict[Path, set[int]] = {}
    current: set[int] = set()
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:].removeprefix("b/")
            current = added.setdefault(root / name, set())
        elif match := HUNK_RE.match(line):
            start, count = int(match[1]), int(match[2] or 1)
            current.update(range(start, start + count))
    return added


def _project_aliases(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "wgsextract_cli"
        ):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if alias.name.startswith("wgsextract_cli")
            )
    return names


def _string(node: ast.AST) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _patched_private(call: ast.Call, project: set[str]) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute) or not call.args:
        return None
    if func.attr == "setattr" or (func.attr == "object" and len(call.args) > 1):
        target = _string(call.args[0])
        if target is not None:
            owner, _, attr = target.rpartition(".")
        elif len(call.args) > 1:
            owner, attr = ast.unparse(call.args[0]), _string(call.args[1]) or ""
        else:
            return None
    elif func.attr == "patch":
        owner, _, attr = (_string(call.args[0]) or "").rpartition(".")
    else:
        return None
    is_project = owner.startswith("wgsextract_cli") or owner.split(".")[0] in project
    private = attr.startswith("_") and not attr.startswith("__")
    return f"{owner}.{attr}" if is_project and private else None


def _is_argv(node: ast.AST) -> bool:
    if not isinstance(node, ast.List) or len(node.elts) < 2:
        return False
    first = _string(node.elts[0]) or ""
    return first.rsplit("/", 1)[-1] in TOOLS


def smells(path: Path, lines: set[int]) -> list[tuple[int, str, str]]:
    source = path.read_text(encoding="utf-8")
    text = source.splitlines()
    tree = ast.parse(source)
    project = _project_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.lineno in lines:
            if target := _patched_private(node, project):
                found.append(
                    (node.lineno, "TS001", f"patches private internal {target}")
                )
        elif isinstance(node, ast.Assert) and node.lineno in lines:
            span = text[node.lineno - 2 : node.end_lineno or node.lineno]
            if "# contract:" in "\n".join(span):
                continue
            if any(_is_argv(sub) for sub in ast.walk(node.test)):
                found.append((node.lineno, "TS002", "asserts a whole tool argv"))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="compare against")
    parser.add_argument(
        "--github", action="store_true", help="print GitHub Actions annotations"
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        changed = added_lines(args.base, args.root)
    except subprocess.CalledProcessError as exc:
        print(f"test-smells: skipped, git failed: {exc.stderr.strip()}")
        return 0
    total = 0
    for path, lines in sorted(changed.items()):
        if not path.exists():
            continue
        rel = path.relative_to(args.root).as_posix()
        for line, code, message in smells(path, lines):
            total += 1
            if args.github:
                print(f"::warning file={rel},line={line},title={code}::{message}")
            else:
                print(f"{rel}:{line}: {code} {message}")
    if total:
        print(
            f"test-smells: {total} advisory finding(s); "
            "see 'Write tests' in docs/developer_setup.md"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
