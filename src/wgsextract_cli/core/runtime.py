import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from pathlib import Path

WSL_TOOL_PREFIX = "wsl:"
RUNTIME_ENV_VAR = "WGSEXTRACT_TOOL_RUNTIME"
VALID_RUNTIME_MODES = {"auto", "native", "wsl"}

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MEMORY_RE = re.compile(r"^\d+(?:\.\d+)?[KMGTP]B?$", re.IGNORECASE)
_REGION_RE = re.compile(r"^(?:chr)?[A-Za-z0-9_.-]+:\d+(?:-\d+)?$")

DEFAULT_WSL_PROCESSOR_RATIO = 2 / 3
DEFAULT_WSL_MEMORY_RATIO = 3 / 4
DEFAULT_WSL_SWAP_RATIO = 1 / 4


@dataclass(frozen=True)
class WSLResourceRecommendation:
    memory: str
    processors: int
    swap: str
    host_memory_gb: int
    host_processors: int


def is_windows_host() -> bool:
    return sys.platform == "win32"


def get_tool_runtime_mode() -> str:
    mode = os.environ.get(RUNTIME_ENV_VAR)
    if not mode:
        try:
            from wgsextract_cli.core.config import settings

            configured = settings.get("tool_runtime")
            mode = str(configured) if configured is not None else None
        except Exception:
            mode = None

    normalized = (mode or "auto").strip().lower()
    if normalized not in VALID_RUNTIME_MODES:
        return "auto"
    return normalized


def should_consider_wsl() -> bool:
    mode = get_tool_runtime_mode()
    return is_windows_host() and mode != "native"


@lru_cache(maxsize=2)
def detect_wsl_available(*, force: bool = False) -> bool:
    if (not force and not should_consider_wsl()) or shutil.which("wsl") is None:
        return False

    try:
        result = subprocess.run(
            ["wsl", "bash", "-lc", "printf ok"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "ok"


def wsl_tool_command(command: str) -> str:
    return f"{WSL_TOOL_PREFIX}{command}"


def is_wsl_tool_command(command: object) -> bool:
    return isinstance(command, str) and command.startswith(WSL_TOOL_PREFIX)


def strip_wsl_tool_prefix(command: str) -> str:
    return command[len(WSL_TOOL_PREFIX) :] if is_wsl_tool_command(command) else command


@lru_cache(maxsize=256)
def wsl_command_available(command: str) -> bool:
    if not detect_wsl_available():
        return False

    try:
        result = subprocess.run(
            ["wsl", "bash", "-lc", f"command -v {shlex.quote(command)}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


@lru_cache(maxsize=256)
def wsl_pixi_tool_available(tool: str, env: str) -> bool:
    if not detect_wsl_available():
        return False

    quoted_tool = shlex.quote(tool)
    quoted_env = shlex.quote(env)
    script = (
        "if [ -x ~/.pixi/bin/pixi ]; then "
        f"~/.pixi/bin/pixi run -e {quoted_env} which {quoted_tool}; "
        "else exit 127; fi"
    )
    try:
        result = subprocess.run(
            ["wsl", "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return False
    return result.returncode == 0


@lru_cache(maxsize=512)
def windows_to_wsl_path(path: str) -> str:
    if not path:
        return path

    normalized = str(Path(path).expanduser())
    if not _WINDOWS_ABS_RE.match(normalized):
        return path

    if detect_wsl_available():
        try:
            result = subprocess.run(
                ["wsl", "wslpath", "-a", "-u", normalized],
                capture_output=True,
                text=True,
                timeout=10,
            )
            converted = result.stdout.replace("\x00", "").strip()
            if result.returncode == 0 and converted:
                return converted
        except Exception:
            pass

    drive = normalized[0].lower()
    rest = normalized[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _looks_like_path_arg(arg: str) -> bool:
    if not arg or arg.startswith("-"):
        return False
    if "://" in arg or _REGION_RE.match(arg) or _MEMORY_RE.match(arg):
        return False
    has_windows_path = bool(_WINDOWS_ABS_RE.search(arg) or "\\" in arg)
    if "=" in arg and not has_windows_path:
        return False
    return has_windows_path


def translate_wsl_arg(arg: str) -> str:
    if not _looks_like_path_arg(arg):
        return arg
    if "=" in arg:
        key, value = arg.split("=", 1)
        if _WINDOWS_ABS_RE.match(value):
            return f"{key}={windows_to_wsl_path(value)}"
        if "\\" in value:
            normalized_value = value.replace("\\", "/")
            return f"{key}={normalized_value}"
    if _WINDOWS_ABS_RE.match(arg):
        return windows_to_wsl_path(arg)
    if "\\" in arg:
        return arg.replace("\\", "/")
    return windows_to_wsl_path(arg)


def translate_wsl_args(args: list[str]) -> list[str]:
    return [translate_wsl_arg(arg) for arg in args]


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def wrap_command(cmd: list[str], force_wsl: bool = False) -> list[str]:
    if not cmd:
        return cmd

    command = cmd[0]
    command_args = cmd
    needs_wsl = force_wsl

    if is_wsl_tool_command(command):
        command_args = shlex.split(strip_wsl_tool_prefix(command)) + cmd[1:]
        needs_wsl = True

    if not needs_wsl:
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
    except Exception:
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


def _read_wslconfig_text(config_path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return config_path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return config_path.read_text(encoding="utf-8", errors="replace")


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
