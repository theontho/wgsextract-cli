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
from unittest.mock import Mock

import pytest

from wgsextract_cli.core import utils
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


@pytest.mark.parametrize("returncode", [0, 2])
def test_windows_timed_wait_preserves_complete_output(monkeypatch, returncode):
    process = Mock(returncode=returncode)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("tool", 0.5, output=b"partial", stderr=b"partial"),
        ("complete stdout", "complete stderr"),
    ]
    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils, "process_group_kwargs", lambda: {})
    monkeypatch.setattr(utils.subprocess, "Popen", lambda *args, **kwargs: process)
    if returncode:
        with pytest.raises(utils.ToolExecutionError) as error:
            run_command([Path(sys.executable)], capture_output=True)
        result = error.value
        assert result.output == "complete stdout"
    else:
        result = run_command([Path(sys.executable)], capture_output=True)
        assert result.stdout == "complete stdout"
    assert result.stderr == "complete stderr"
    assert process.communicate.call_count == 2
    assert all(
        call.kwargs == {"timeout": 0.5} for call in process.communicate.call_args_list
    )


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_interrupt_keeps_tree_cleanup_platform_specific(monkeypatch, platform):
    process = Mock(returncode=None)
    process.communicate.side_effect = (
        [subprocess.TimeoutExpired("tool", 0.5), KeyboardInterrupt]
        if platform == "win32"
        else KeyboardInterrupt
    )
    cleaned = []

    def cleanup():
        assert any(process in group for group in utils.proc_registry.processes.values())
        cleaned.append(process)

    monkeypatch.setattr(utils.sys, "platform", platform)
    monkeypatch.setattr(utils, "process_group_kwargs", lambda: {})
    monkeypatch.setattr(utils.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(utils, "cleanup_processes", cleanup)
    with pytest.raises(KeyboardInterrupt):
        run_command([Path(sys.executable)], capture_output=True)
    assert cleaned == ([process] if platform == "win32" else [])
    assert not any(process in group for group in utils.proc_registry.processes.values())


def test_unix_communicate_keeps_original_call(monkeypatch):
    process = Mock(returncode=0)
    process.communicate.return_value = ("stdout", "stderr")
    monkeypatch.setattr(utils.sys, "platform", "linux")
    monkeypatch.setattr(utils, "process_group_kwargs", lambda: {})
    monkeypatch.setattr(utils.subprocess, "Popen", lambda *args, **kwargs: process)
    result = run_command([Path(sys.executable)], capture_output=True)
    assert (result.stdout, result.stderr) == ("stdout", "stderr")
    process.communicate.assert_called_once_with()


def test_windows_timeout_retries_drain_both_real_pipes(monkeypatch, tmp_path):
    original = subprocess.Popen
    children = []
    waits = []
    release = tmp_path / "release"

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        communicate = child.communicate

        def bounded(*args, **kwargs):
            waits.append(kwargs.get("timeout"))
            try:
                return communicate(*args, **kwargs)
            except subprocess.TimeoutExpired:
                release.touch()
                raise

        child.communicate = bounded
        return child

    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils, "process_group_kwargs", lambda: {})
    monkeypatch.setattr(utils.subprocess, "Popen", launch)
    command = [
        Path(sys.executable),
        "-c",
        "import pathlib,sys,time;"
        "sys.stdout.buffer.write(('o'*200000+'☃').encode());sys.stdout.flush();"
        "sys.stderr.buffer.write(('e'*200000+'☃').encode());sys.stderr.flush();"
        "release=pathlib.Path(sys.argv[1]);deadline=time.monotonic()+10;"
        "\nwhile not release.exists() and time.monotonic()<deadline:time.sleep(0.01)"
        "\nassert release.exists(),'communicate timeout did not release child'",
        str(release),
    ]
    try:
        result = run_command(command, capture_output=True)
        assert result.stdout == "o" * 200000 + "☃"
        assert result.stderr == "e" * 200000 + "☃"
        assert len(waits) >= 2 and all(timeout == 0.5 for timeout in waits)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
