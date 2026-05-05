import os
import platform
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


def runtime_root() -> Path:
    configured = os.environ.get(RUNTIME_DIR_ENV_VAR)
    if not configured:
        try:
            from wgsextract_cli.core.config import settings

            config_value = settings.get("runtime_directory")
            configured = str(config_value) if config_value else None
        except Exception:
            configured = None
    return (
        Path(configured).expanduser().resolve()
        if configured
        else default_runtime_root()
    )


def default_runtime_root() -> Path:
    root = repo_root()
    if (root / ".git").exists():
        return root / "runtime"

    try:
        from platformdirs import user_data_path

        return user_data_path("wgsextract-cli", "WGSExtract") / "runtime"
    except Exception:
        return Path.home() / ".wgsextract" / "runtime"


def bundled_runtime_spec(mode: str) -> WindowsRuntimeSpec:
    try:
        return WINDOWS_RUNTIME_SPECS[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown bundled runtime: {mode}") from exc


def bundled_runtime_dir(mode: str) -> Path:
    return runtime_root() / bundled_runtime_spec(mode).dirname


def bundled_runtime_bash(mode: str) -> Path:
    spec = bundled_runtime_spec(mode)
    return bundled_runtime_dir(mode) / Path(spec.bash_relpath)


def bundled_runtime_path_entries(mode: str) -> list[Path]:
    root = bundled_runtime_dir(mode)
    return [
        root / Path(relpath) for relpath in bundled_runtime_spec(mode).path_relpaths
    ]


def bundled_runtime_path(mode: str) -> str:
    return ":".join(
        windows_runtime_path(str(path)) for path in bundled_runtime_path_entries(mode)
    )


def bundled_runtime_shell_path(mode: str) -> str:
    spec = bundled_runtime_spec(mode)
    return ":".join(
        "/" + relpath.replace("\\", "/").strip("/") for relpath in spec.path_relpaths
    )


def pacman_ucrt64_bin_dirs() -> list[Path]:
    configured_paths: list[str] = []
    for env_name in (PACMAN_UCRT64_BIN_ENV_VAR, "UCRT64_BIN"):
        env_value = os.environ.get(env_name)
        if env_value:
            configured_paths.append(env_value)
    try:
        from wgsextract_cli.core.config import settings

        config_value = settings.get("pacman_ucrt64_bin")
        if config_value:
            configured_paths.append(str(config_value))
    except Exception:
        pass

    candidates: list[Path | None] = [
        Path(path).expanduser() for path in configured_paths
    ]
    candidates.extend(
        [
            Path(os.environ.get("MSYS2_ROOT", "")) / "ucrt64" / "bin"
            if os.environ.get("MSYS2_ROOT")
            else None,
            Path("C:/msys64/ucrt64/bin"),
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "msys64"
            / "ucrt64"
            / "bin"
            if os.environ.get("LOCALAPPDATA")
            else None,
        ]
    )

    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        normalized = candidate.resolve() if candidate.exists() else candidate
        key = str(normalized).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def pacman_usr_bin_dirs() -> list[Path]:
    candidates = []
    for ucrt64_bin in pacman_ucrt64_bin_dirs():
        if ucrt64_bin.name.lower() != "bin":
            continue
        ucrt64_dir = ucrt64_bin.parent
        if ucrt64_dir.name.lower() != "ucrt64":
            continue
        candidates.append(ucrt64_dir.parent / "usr" / "bin")
    return _dedupe_paths(candidates)


def pacman_tool_bin_dirs() -> list[Path]:
    return _dedupe_paths([*pacman_ucrt64_bin_dirs(), *pacman_usr_bin_dirs()])


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        normalized = path.resolve() if path.exists() else path
        key = str(normalized).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def is_pacman_tool_path(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    try:
        candidate = Path(path).resolve()
    except OSError:
        candidate = Path(path)
    normalized = str(candidate).replace("\\", "/").lower()
    if "/ucrt64/bin/" in normalized or normalized.endswith("/ucrt64/bin"):
        return True
    for tool_dir in pacman_tool_bin_dirs():
        try:
            tool_root = tool_dir.resolve()
        except OSError:
            tool_root = tool_dir
        try:
            if candidate == tool_root or candidate.is_relative_to(tool_root):
                return True
        except ValueError:
            continue
    return False


@lru_cache(maxsize=512)
def pacman_tool_path(command: str) -> str | None:
    names = [command]
    if Path(command).suffix.lower() != ".exe":
        names.append(f"{command}.exe")

    for name in names:
        path = shutil.which(name)
        if path and is_pacman_tool_path(path):
            return path

    for tool_dir in pacman_tool_bin_dirs():
        for name in names:
            candidate = tool_dir / name
            if candidate.exists():
                return str(candidate)
    return None


@lru_cache(maxsize=512)
def pacman_tool_available(command: str) -> bool:
    return pacman_tool_path(command) is not None


@lru_cache(maxsize=8)
def detect_bundled_runtime_available(mode: str, *, force: bool = False) -> bool:
    if not force and not should_consider_bundled_runtime(mode):
        return False

    bash = bundled_runtime_bash(mode)
    if not bash.exists():
        return False

    try:
        result = subprocess.run(
            [str(bash), "-lc", _bundled_shell_prelude(mode) + "printf ok"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "ok"


@lru_cache(maxsize=512)
def bundled_command_available(mode: str, command: str) -> bool:
    if not detect_bundled_runtime_available(mode, force=True):
        return False

    try:
        result = subprocess.run(
            [
                str(bundled_runtime_bash(mode)),
                "-lc",
                _bundled_shell_prelude(mode) + f"command -v {shlex.quote(command)}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


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


def windows_runtime_path(path: str) -> str:
    if not path:
        return path
    return str(Path(path).expanduser()).replace("\\", "/")


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


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _bundled_shell_prelude(mode: str) -> str:
    spec = bundled_runtime_spec(mode)
    exports = [f"export {assignment}" for assignment in spec.shell_exports]
    exports.append(f"export PATH={shlex.quote(bundled_runtime_shell_path(mode))}:$PATH")
    return "; ".join(exports) + "; "


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
        return [strip_pacman_tool_prefix(command)] + cmd[1:]

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
