import os
import shlex
from math import ceil
from pathlib import Path

from .runtime import (
    _WINDOWS_ABS_RE,
    DEFAULT_WSL_MEMORY_RATIO,
    DEFAULT_WSL_PROCESSOR_RATIO,
    DEFAULT_WSL_SWAP_RATIO,
    WSLResourceRecommendation,
    bundled_tool_command_mode,
    is_pacman_tool_command,
    is_windows_host,
    is_wsl_tool_command,
    strip_bundled_tool_prefix,
    strip_pacman_tool_prefix,
    strip_wsl_tool_prefix,
)
from .runtime_paths import (
    _bundled_shell_prelude,
    _looks_like_path_arg,
    bundled_runtime_bash,
    translate_wsl_args,
    windows_runtime_path,
    windows_to_wsl_path,
)


def translate_windows_runtime_arg(arg: str) -> str:
    if not _looks_like_path_arg(arg):
        return arg
    if "=" in arg:
        key, value = arg.split("=", 1)
        if _WINDOWS_ABS_RE.match(value) or "\\" in value:
            return f"{key}={windows_runtime_path(value)}"
    if _WINDOWS_ABS_RE.match(arg) or "\\" in arg:
        return windows_runtime_path(arg)
    return arg


def translate_windows_runtime_args(args: list[str]) -> list[str]:
    return [translate_windows_runtime_arg(arg) for arg in args]


def translate_pacman_arg(arg: str) -> str:
    if is_windows_host() and arg == "/dev/null":
        return "NUL"
    return arg


def translate_pacman_args(args: list[str]) -> list[str]:
    return [translate_pacman_arg(arg) for arg in args]


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def wrap_command(cmd: list[str], force_wsl: bool = False) -> list[str]:
    if not cmd:
        return cmd

    command = cmd[0]
    command_args = cmd
    needs_wsl = force_wsl
    bundled_mode = bundled_tool_command_mode(command)

    if is_wsl_tool_command(command):
        command_args = shlex.split(strip_wsl_tool_prefix(command)) + cmd[1:]
        needs_wsl = True

    if is_pacman_tool_command(command):
        return [strip_pacman_tool_prefix(command)] + translate_pacman_args(cmd[1:])

    if bundled_mode:
        command_args = shlex.split(strip_bundled_tool_prefix(command)) + cmd[1:]

    if not needs_wsl:
        if bundled_mode:
            translated = translate_windows_runtime_args(command_args)
            script = _bundled_shell_prelude(bundled_mode) + shell_join(translated)

            cwd = os.getcwd()
            if _WINDOWS_ABS_RE.match(cwd):
                script = (
                    _bundled_shell_prelude(bundled_mode)
                    + f"cd {shlex.quote(windows_runtime_path(cwd))} && "
                    + shell_join(translated)
                )

            return [str(bundled_runtime_bash(bundled_mode)), "-lc", script]
        return command_args

    translated = translate_wsl_args(command_args)
    script = shell_join(translated)

    cwd = os.getcwd()
    if _WINDOWS_ABS_RE.match(cwd):
        script = f"cd {shlex.quote(windows_to_wsl_path(cwd))} && {script}"

    return ["wsl", "bash", "-lc", script]


def get_wslconfig_path() -> Path:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / ".wslconfig"


def _host_memory_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except (ImportError, OSError, AttributeError):
        return None


def _format_gb(value: int) -> str:
    return f"{max(1, value)}GB"


def recommend_wslconfig_settings(
    *,
    host_processors: int | None = None,
    host_memory_bytes: int | None = None,
) -> WSLResourceRecommendation:
    """Return benchmark-backed default WSL2 resource settings for this host."""
    processors_total = host_processors or os.cpu_count() or 1
    memory_total_bytes = host_memory_bytes or _host_memory_bytes()

    if memory_total_bytes is None:
        host_memory_gb = 16
    else:
        host_memory_gb = max(1, round(memory_total_bytes / (1024**3)))

    recommended_processors = max(
        1, min(processors_total, round(processors_total * DEFAULT_WSL_PROCESSOR_RATIO))
    )
    recommended_memory_gb = min(
        host_memory_gb, max(1, ceil(host_memory_gb * DEFAULT_WSL_MEMORY_RATIO))
    )
    recommended_swap_gb = max(1, ceil(host_memory_gb * DEFAULT_WSL_SWAP_RATIO))

    return WSLResourceRecommendation(
        memory=_format_gb(recommended_memory_gb),
        processors=recommended_processors,
        swap=_format_gb(recommended_swap_gb),
        host_memory_gb=host_memory_gb,
        host_processors=processors_total,
    )


def _read_wslconfig_text(config_path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return config_path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return config_path.read_text(encoding="utf-8", errors="replace")


def read_wslconfig_settings(path: Path | None = None) -> dict[str, str]:
    config_path = path or get_wslconfig_path()
    if not config_path.exists():
        return {}

    settings: dict[str, str] = {}
    in_wsl2 = False
    for raw_line in _read_wslconfig_text(config_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_wsl2 = line.lower() == "[wsl2]"
            continue
        if in_wsl2 and "=" in line:
            key, value = line.split("=", 1)
            settings[key.strip().lower()] = value.strip()
    return settings


def write_wslconfig_settings(
    *,
    memory: str | None = None,
    processors: int | None = None,
    swap: str | None = None,
    path: Path | None = None,
) -> Path:
    config_path = path or get_wslconfig_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _read_wslconfig_text(config_path) if config_path.exists() else ""
    updates: dict[str, str] = {}
    if memory:
        updates["memory"] = memory
    if processors is not None:
        updates["processors"] = str(processors)
    if swap:
        updates["swap"] = swap

    lines = existing.splitlines()
    output: list[str] = []
    in_wsl2 = False
    saw_wsl2 = False
    applied = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_wsl2:
                for key, value in updates.items():
                    if key not in applied:
                        output.append(f"{key}={value}")
                        applied.add(key)
            in_wsl2 = stripped.lower() == "[wsl2]"
            saw_wsl2 = saw_wsl2 or in_wsl2
            output.append(line)
            continue

        if in_wsl2 and "=" in stripped:
            key = stripped.split("=", 1)[0].strip().lower()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                applied.add(key)
                continue
        output.append(line)

    if in_wsl2:
        for key, value in updates.items():
            if key not in applied:
                output.append(f"{key}={value}")
                applied.add(key)

    if not saw_wsl2:
        if output and output[-1].strip():
            output.append("")
        output.append("[wsl2]")
        for key, value in updates.items():
            output.append(f"{key}={value}")

    config_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    return config_path
