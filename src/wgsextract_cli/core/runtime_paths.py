import logging
import os
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .runtime import (
    _MEMORY_RE,
    _REGION_RE,
    _WINDOWS_ABS_RE,
    PACMAN_UCRT64_BIN_ENV_VAR,
    RUNTIME_DIR_ENV_VAR,
    WINDOWS_RUNTIME_SPECS,
    WindowsRuntimeSpec,
    default_runtime_root,
    detect_wsl_available,
    should_consider_bundled_runtime,
)


def runtime_root() -> Path:
    configured = os.environ.get(RUNTIME_DIR_ENV_VAR)
    if not configured:
        try:
            from wgsextract_cli.core.config import settings

            config_value = settings.get("runtime_directory")
            configured = str(config_value) if config_value else None
        except (ImportError, AttributeError):
            logging.debug("Runtime directory setting could not be read.", exc_info=True)
            configured = None
    return (
        Path(configured).expanduser().resolve()
        if configured
        else default_runtime_root()
    )


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


def windows_runtime_path(path: str) -> str:
    if not path:
        return path
    return str(Path(path).expanduser()).replace("\\", "/")


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
    except (ImportError, AttributeError):
        logging.debug("Pacman UCRT64 bin setting could not be read.", exc_info=True)

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


def _bundled_shell_prelude(mode: str) -> str:
    spec = bundled_runtime_spec(mode)
    exports = [f"export {assignment}" for assignment in spec.shell_exports]
    exports.append(f"export PATH={shlex.quote(bundled_runtime_shell_path(mode))}:$PATH")
    return "; ".join(exports) + "; "


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
    except (OSError, subprocess.SubprocessError):
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
    except (OSError, subprocess.SubprocessError):
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
    except (OSError, subprocess.SubprocessError):
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
    except (OSError, subprocess.SubprocessError):
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
        except (OSError, subprocess.SubprocessError):
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
