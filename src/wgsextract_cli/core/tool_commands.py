"""Split a resolved tool command (a path or a wrapper command line) into argv."""

import ctypes
import shlex

from wgsextract_cli.core import runtime
from wgsextract_cli.core.paths import command_is_path, expand_path


def _tool_command_parts(cmd_base: str) -> list[str]:
    """Split wrapper commands without corrupting native executable paths."""
    normalized_cmd = expand_path(cmd_base)
    if command_is_path(normalized_cmd):
        return [normalized_cmd]
    if runtime.is_windows_host():
        return _windows_command_line_split(normalized_cmd)
    return shlex.split(normalized_cmd)


def _split_windows_argv(command_line: str) -> list[str]:
    """CommandLineToArgvW's rules (the inverse of subprocess.list2cmdline), for
    when the Windows API isn't available."""
    args: list[str] = []
    current: list[str] = []
    in_token = in_quotes = False
    i = 0
    while i < len(command_line):
        char = command_line[i]
        if char == "\\":
            run_end = i
            while run_end < len(command_line) and command_line[run_end] == "\\":
                run_end += 1
            count = run_end - i
            if run_end < len(command_line) and command_line[run_end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    run_end += 1
            else:
                current.append("\\" * count)
            in_token = True
            i = run_end
            continue
        if char == '"':
            in_quotes = not in_quotes
            in_token = True
        elif char in " \t" and not in_quotes:
            if in_token:
                args.append("".join(current))
                current, in_token = [], False
        else:
            current.append(char)
            in_token = True
        i += 1
    if in_token:
        args.append("".join(current))
    return args


def _windows_command_line_split(command_line: str) -> list[str]:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return _split_windows_argv(command_line)

    argc = ctypes.c_int()
    command_line_to_argv = windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(command_line, ctypes.byref(argc))
    if not argv:
        raise ValueError(f"Could not parse Windows command line: {command_line}")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        windll.kernel32.LocalFree(argv)
