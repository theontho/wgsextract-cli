"""Text files the CLI writes end lines with \\n on every platform.

Python's text mode writes \\r\\n on Windows unless `newline=` says otherwise, so
the same command made different files there: microarray kits with \\r\\n headers
over \\n genotype rows, and region and target lists that htslib tools read. Every
text-mode write in the package must pass `newline=` (normally "\\n").
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "wgsextract_cli"
WRITE_MODES = {"w", "a", "x", "w+", "a+", "x+", "r+", "wt", "at", "xt"}


def _mode(call: ast.Call, position: int) -> str | None:
    """The call's mode argument, when it's a string literal."""
    for keyword in call.keywords:
        if keyword.arg == "mode":
            node: ast.expr | None = keyword.value
            break
    else:
        node = call.args[position] if len(call.args) > position else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _text_writes_without_newline(tree: ast.AST) -> list[int]:
    lines = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if any(keyword.arg == "newline" for keyword in call.keywords):
            continue
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "write_text":
            lines.append(call.lineno)
        elif name in {"open", "fdopen"}:
            # open(path, mode), os.fdopen(fd, mode), Path(...).open(mode),
            # gzip.open(path, mode).
            path_method = isinstance(func, ast.Attribute) and not (
                isinstance(func.value, ast.Name)
                and func.value.id in {"os", "io", "gzip", "bz2", "lzma", "codecs"}
            )
            mode = _mode(call, 0 if path_method else 1)
            if mode in WRITE_MODES:
                lines.append(call.lineno)
    return lines


def test_text_writes_choose_their_line_endings() -> None:
    offenders = []
    for source in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
        offenders += [
            f"{source.relative_to(PACKAGE.parent)}:{line}"
            for line in _text_writes_without_newline(tree)
        ]
    assert not offenders, (
        "text-mode writes without newline= (they write \\r\\n on Windows):\n"
        + "\n".join(offenders)
    )


def test_checker_finds_each_form() -> None:
    tree = ast.parse(
        "open(p, 'w')\n"
        "open(p, mode='a')\n"
        "os.fdopen(fd, 'w')\n"
        "Path(p).open('w')\n"
        "gzip.open(p, 'wt')\n"
        "p.write_text(s)\n"
        "open(p, 'w', newline='\\n')\n"
        "open(p, 'wb')\n"
        "open(p)\n"
        "p.write_text(s, newline='\\n')\n"
    )
    assert _text_writes_without_newline(tree) == [1, 2, 3, 4, 5, 6]
