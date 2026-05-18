import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

WSL_TOOL_PREFIX = "wsl:"


PACMAN_TOOL_PREFIX = "pacman:"


RUNTIME_ENV_VAR = "WGSEXTRACT_TOOL_RUNTIME"


RUNTIME_DIR_ENV_VAR = "WGSEXTRACT_RUNTIME_DIR"


PACMAN_UCRT64_BIN_ENV_VAR = "WGSEXTRACT_PACMAN_UCRT64_BIN"


VALID_RUNTIME_MODES = {"auto", "native", "wsl", "cygwin", "msys2", "pacman"}


BUNDLED_RUNTIME_MODES = {"cygwin", "msys2"}


_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


_MEMORY_RE = re.compile(r"^\d+(?:\.\d+)?[KMGTP]B?$", re.IGNORECASE)


_REGION_RE = re.compile(r"^(?:chr)?[A-Za-z0-9_.-]+:\d+(?:-\d+)?$")


DEFAULT_WSL_PROCESSOR_RATIO = 2 / 3


DEFAULT_WSL_THREAD_RATIO = 3 / 4


DEFAULT_WSL_MEMORY_RATIO = 3 / 4


DEFAULT_WSL_SWAP_RATIO = 1 / 4


MACOS_PERFORMANCE_LEVEL_NAMES = {"performance", "super"}


@dataclass(frozen=True)
class WSLResourceRecommendation:
    memory: str
    processors: int
    swap: str
    host_memory_gb: int
    host_processors: int


@dataclass(frozen=True)
class ThreadTuningProfile:
    threads: int
    label: str
    reason: str


@dataclass(frozen=True)
class WindowsRuntimeSpec:
    mode: str
    dirname: str
    display_name: str
    archive_key: str
    bash_relpath: str
    path_relpaths: tuple[str, ...]
    shell_exports: tuple[str, ...] = ()


WINDOWS_RUNTIME_SPECS: dict[str, WindowsRuntimeSpec] = {
    "cygwin": WindowsRuntimeSpec(
        mode="cygwin",
        dirname="cygwin64",
        display_name="Cygwin64",
        archive_key="cygwin64",
        bash_relpath="bin/bash.exe",
        path_relpaths=("usr/local/bin", "bin", "jre8/bin", "FastQC"),
    ),
    "msys2": WindowsRuntimeSpec(
        mode="msys2",
        dirname="msys2",
        display_name="MSYS2 UCRT64",
        archive_key="msys2",
        bash_relpath="usr/bin/bash.exe",
        path_relpaths=("ucrt64/bin", "usr/bin", "jre8/bin", "FastQC"),
        shell_exports=("MSYSTEM=UCRT64", "CHERE_INVOKING=1"),
    ),
}


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
    return is_windows_host() and mode in {"auto", "wsl"}


def should_consider_bundled_runtime(mode: str | None = None) -> bool:
    selected = mode or get_tool_runtime_mode()
    return is_windows_host() and selected in BUNDLED_RUNTIME_MODES


def should_consider_pacman_runtime(mode: str | None = None) -> bool:
    selected = mode or get_tool_runtime_mode()
    return is_windows_host() and selected == "pacman"


def _sysctl_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _macos_named_performance_core_count() -> int | None:
    output = _sysctl_output(["sysctl", "-a"])
    if output is None:
        return None

    perf_levels: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            key, separator, value = line.partition("=")
        if not separator:
            continue

        key_parts = key.strip().split(".")
        if len(key_parts) != 3 or key_parts[0] != "hw":
            continue
        level_key, field = key_parts[1], key_parts[2]
        if not level_key.startswith("perflevel") or field not in {
            "name",
            "physicalcpu",
        }:
            continue
        perf_levels.setdefault(level_key, {})[field] = value.strip().strip('"')

    total = 0
    for level_fields in perf_levels.values():
        if (
            level_fields.get("name", "").strip().lower()
            not in MACOS_PERFORMANCE_LEVEL_NAMES
        ):
            continue
        core_count = _positive_int(level_fields.get("physicalcpu", ""))
        if core_count:
            total += core_count
    return total or None


def macos_performance_core_count() -> int | None:
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        return None

    named_core_count = _macos_named_performance_core_count()
    if named_core_count:
        return named_core_count

    output = _sysctl_output(["sysctl", "-n", "hw.perflevel0.physicalcpu"])
    if output is None:
        return None
    return _positive_int(output.strip())


def default_thread_tuning_profile() -> ThreadTuningProfile:
    """Return the central default thread policy for user-facing commands."""
    apple_perf_cores = macos_performance_core_count()
    if apple_perf_cores:
        return ThreadTuningProfile(
            threads=apple_perf_cores,
            label=str(apple_perf_cores),
            reason="Apple Silicon performance-core count",
        )

    total_cpus = os.cpu_count() or 4
    if should_consider_wsl():
        threads = max(1, int(total_cpus * DEFAULT_WSL_THREAD_RATIO))
        return ThreadTuningProfile(
            threads=threads,
            label=str(threads),
            reason="WSL balanced CPU allocation",
        )

    return ThreadTuningProfile(
        threads=max(1, total_cpus),
        label=str(max(1, total_cpus)),
        reason="all available cores",
    )


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


def bundled_tool_prefix(mode: str) -> str:
    return f"{mode}:"


def bundled_tool_command(mode: str, command: str) -> str:
    return f"{bundled_tool_prefix(mode)}{command}"


def bundled_tool_command_mode(command: object) -> str | None:
    if not isinstance(command, str):
        return None
    for mode in BUNDLED_RUNTIME_MODES:
        if command.startswith(bundled_tool_prefix(mode)):
            return mode
    return None


def is_bundled_tool_command(command: object) -> bool:
    return bundled_tool_command_mode(command) is not None


def strip_bundled_tool_prefix(command: str) -> str:
    mode = bundled_tool_command_mode(command)
    if not mode:
        return command
    return command[len(bundled_tool_prefix(mode)) :]


def pacman_tool_command(command: str) -> str:
    return f"{PACMAN_TOOL_PREFIX}{command}"


def is_pacman_tool_command(command: object) -> bool:
    return isinstance(command, str) and command.startswith(PACMAN_TOOL_PREFIX)


def strip_pacman_tool_prefix(command: str) -> str:
    return (
        command[len(PACMAN_TOOL_PREFIX) :]
        if is_pacman_tool_command(command)
        else command
    )


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return current.parents[3]


def default_runtime_root() -> Path:
    root = repo_root()
    if (root / ".git").exists():
        return root / "runtime"

    try:
        from platformdirs import user_data_path

        return user_data_path("wgsextract-cli", "WGSExtract") / "runtime"
    except Exception:
        return Path.home() / ".wgsextract" / "runtime"
