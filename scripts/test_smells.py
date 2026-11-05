#!/usr/bin/env python3
"""Flag change-detector patterns in test lines added since a base revision.

Untracked test files count as wholly added.

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
ARGV_ASSERTS = {
    "assertEqual",
    "assertListEqual",
    "assert_any_call",
    "assert_called_once_with",
    "assert_called_with",
}


def _git(*args: str, cwd: Path) -> str:
    # quotePath=false keeps non-ASCII paths unquoted in diff headers and ls-files.
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout


def added_lines(base: str, root: Path) -> dict[Path, set[int] | None]:
    """Map changed test files to their added lines; ``None`` means every line."""
    merge_base = _git("merge-base", base, "HEAD", cwd=root).strip()
    diff = _git(
        *("diff", "--no-color", "--no-ext-diff"),
        *("--src-prefix=a/", "--dst-prefix=b/", "-U0", "--diff-filter=AMR"),
        *(merge_base, "--", "tests/*.py"),
        cwd=root,
    )
    added: dict[Path, set[int] | None] = {}
    current: set[int] = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = set()
            added[root / line[6:]] = current
        elif match := HUNK_RE.match(line):
            start, count = int(match[1]), int(match[2] or 1)
            current.update(range(start, start + count))
    untracked = _git(
        "ls-files", "--others", "--exclude-standard", "--", "tests/*.py", cwd=root
    )
    added.update((root / name, None) for name in untracked.splitlines())
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
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if not call.args:
        return None
    if name == "setattr" or (name == "object" and len(call.args) > 1):
        target = _string(call.args[0])
        if target is not None:
            owner, _, attr = target.rpartition(".")
        elif len(call.args) > 1:
            owner, attr = ast.unparse(call.args[0]), _string(call.args[1]) or ""
        else:
            return None
    elif name == "patch":
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


def _asserts_argv(stmt: ast.stmt) -> bool:
    """True when an assert compares, or a mock/unittest assert checks, an argv."""
    if isinstance(stmt, ast.Assert):
        return any(
            isinstance(node, ast.Compare)
            and any(_is_argv(side) for side in (node.left, *node.comparators))
            for node in ast.walk(stmt.test)
        )
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        func = stmt.value.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in ARGV_ASSERTS
            and any(_is_argv(arg) for arg in stmt.value.args)
        )
    return False


def _span(node: ast.stmt | ast.expr) -> range:
    return range(node.lineno, (node.end_lineno or node.lineno) + 1)


def smells(path: Path, lines: set[int] | None) -> list[tuple[int, str, str]]:
    source = path.read_text(encoding="utf-8")
    text = source.splitlines()
    tree = ast.parse(source)
    project = _project_aliases(tree)

    def is_added(node: ast.stmt | ast.expr) -> bool:
        return lines is None or any(n in lines for n in _span(node))

    def has_contract(stmt: ast.stmt) -> bool:
        own = text[stmt.lineno - 1 : stmt.end_lineno]
        above = text[stmt.lineno - 2].strip() if stmt.lineno > 1 else ""
        return above.startswith("# contract:") or any("# contract:" in s for s in own)

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_added(node):
            if target := _patched_private(node, project):
                found.append(
                    (node.lineno, "TS001", f"patches private internal {target}")
                )
        elif isinstance(node, ast.stmt) and is_added(node) and _asserts_argv(node):
            if not has_contract(node):
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
        rel = path.relative_to(args.root).as_posix()
        try:
            found = smells(path, lines)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"test-smells: skipped {rel}: {exc}")
            continue
        for line, code, message in found:
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
