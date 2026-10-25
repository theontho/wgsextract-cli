"""Reading a tool's output as text never fails on bytes that aren't UTF-8.

On Windows, Python runs in UTF-8 mode (pixi.toml), so text-mode pipes decode
UTF-8, strictly by default; but Windows' own messages, and some tools', come in
the console code page. A `deps check` died with "'utf-8' codec can't decode
byte 0xf6" that way. Text-mode subprocess calls pass errors= (the wrappers in
core.utils, popen and run_command, do it themselves).
"""

import ast
import subprocess
import sys
from pathlib import Path

from wgsextract_cli.core.utils import popen, run_command

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "wgsextract_cli"


def _undecodable_text_calls(tree: ast.AST) -> list[int]:
    lines = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            continue
        keywords = {k.arg: k.value for k in call.keywords}
        text = keywords.get("text", keywords.get("universal_newlines"))
        if (
            isinstance(text, ast.Constant)
            and text.value is True
            and "errors" not in keywords
        ):
            lines.append(call.lineno)
    return lines


def test_text_mode_subprocesses_tolerate_other_encodings() -> None:
    offenders = []
    for source in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), str(source))
        offenders += [
            f"{source.relative_to(PACKAGE.parent)}:{line}"
            for line in _undecodable_text_calls(tree)
        ]
    assert not offenders, "text=True without errors=:\n" + "\n".join(offenders)


def test_checker_finds_each_form() -> None:
    tree = ast.parse(
        "subprocess.run(c, text=True)\n"
        "subprocess.Popen(c, universal_newlines=True)\n"
        "subprocess.run(c, text=True, errors='replace')\n"
        "subprocess.run(c)\n"
        "popen(c, text=True)\n"
        "helper(c, text=True)\n"
    )
    assert _undecodable_text_calls(tree) == [1, 2]


# A tool printing a cp1252 "ö" (0xf6), which isn't valid UTF-8.
_NOT_UTF8 = [
    sys.executable,
    "-c",
    "import sys; sys.stdout.buffer.write(b'K\\xf6ln\\n')",
]


def test_wrappers_replace_undecodable_bytes() -> None:
    result = run_command(_NOT_UTF8, capture_output=True)
    assert result.stdout == "K�ln\n"
    proc = popen(_NOT_UTF8, stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate()
    assert out == "K�ln\n"
