import shutil
from functools import lru_cache
from pathlib import Path

from wgsextract_cli.core import (
    runtime,
    runtime_paths,
)
from wgsextract_cli.core.paths import expand_path
from wgsextract_cli.core.pixi_tools import (
    _host_pixi_tool_command,
    _wsl_pixi_path,
    is_pixi_tool_command,
    is_pixi_tool_path,
    pixi_env_prefix,
    pixi_env_tool,
)
from wgsextract_cli.core.tool_catalog import (
    MANDATORY_TOOLS,
    OPTIONAL_TOOLS,
    PIXI_TOOL_ENVS,
    _tool_executable_names,
    optional_dependency_tools,
    required_dependency_tools,
)

__all__ = [
    "MANDATORY_TOOLS",
    "OPTIONAL_TOOLS",
    "PIXI_TOOL_ENVS",
    "is_pixi_tool_command",
    "is_pixi_tool_path",
    "optional_dependency_tools",
    "pixi_env_prefix",
    "pixi_env_tool",
    "required_dependency_tools",
]


def get_tool_runtime(path: str | None) -> str:
    """Return the runtime used for a resolved dependency path."""
    if path is None:
        return "missing"
    if runtime.is_wsl_tool_command(path):
        return "wsl"
    bundled_mode = runtime.bundled_tool_command_mode(path)
    if bundled_mode:
        return bundled_mode
    if runtime.is_pacman_tool_command(path) or runtime_paths.is_pacman_tool_path(path):
        return "pacman"
    if is_pixi_tool_path(path) or is_pixi_tool_command(path):
        return "pixi"
    return "native"


def get_jar_dir() -> str:
    """Returns the directory where JAR tools are expected to live."""
    from wgsextract_cli.core.config import settings

    configured = settings.get("jar_directory")
    if configured and isinstance(configured, str):
        expanded_path = expand_path(configured)
        if Path(expanded_path).is_dir():
            return expanded_path

    return str(runtime.repo_root() / "jartools")


def _candidate_bundled_runtime_modes(runtime_mode: str) -> tuple[str, ...]:
    if runtime_mode in runtime.BUNDLED_RUNTIME_MODES:
        return (runtime_mode,)
    if runtime_mode == "auto" and runtime.is_windows_host():
        return ("cygwin", "msys2")
    return ()


def _which_tool(tool: str) -> str | None:
    for executable_name in _tool_executable_names(tool):
        path = shutil.which(executable_name)
        if path:
            return path
    return None


def get_tool_path(tool: str, runtime_mode: str | None = None) -> str | None:
    """Return how to invoke a tool (path or wrapper command), or None if missing.

    Results are cached per process (resolution may spawn ``pixi run`` probes);
    ``clear_tool_caches`` drops them, and config reloads call it.
    """
    return _resolve_tool_path(tool, runtime_mode or runtime.get_tool_runtime_mode())


def clear_tool_caches() -> None:
    _resolve_tool_path.cache_clear()
    runtime_paths.clear_caches()


@lru_cache(maxsize=256)
def _resolve_tool_path(tool: str, runtime_mode: str) -> str | None:
    should_consider_wsl = runtime.should_consider_wsl(runtime_mode)
    prefer_wsl = runtime_mode == "wsl"
    bundled_modes = _candidate_bundled_runtime_modes(runtime_mode)

    if runtime_mode == "pacman":
        pacman_path = runtime_paths.pacman_tool_path(tool)
        if pacman_path:
            return runtime.pacman_tool_command(pacman_path)
        return None

    if runtime_mode == "windows":
        path = _which_tool(tool)
        if path:
            return path
        pacman_path = runtime_paths.pacman_tool_path(tool)
        if pacman_path:
            return runtime.pacman_tool_command(pacman_path)
        return _host_pixi_tool_command(tool)

    if runtime_mode in runtime.BUNDLED_RUNTIME_MODES:
        for mode in bundled_modes:
            if runtime_paths.bundled_command_available(mode, tool):
                return runtime.bundled_tool_command(mode, tool)
        return None

    if should_consider_wsl and prefer_wsl:
        for executable_name in _tool_executable_names(tool):
            if runtime_paths.wsl_command_available(executable_name):
                return runtime.wsl_tool_command(executable_name)

    if prefer_wsl:
        if tool in PIXI_TOOL_ENVS:
            for executable_name in _tool_executable_names(tool):
                if runtime_paths.wsl_pixi_tool_available(
                    executable_name, PIXI_TOOL_ENVS[tool]
                ):
                    return runtime.wsl_tool_command(
                        f"{_wsl_pixi_path()} run -e {PIXI_TOOL_ENVS[tool]} {executable_name}"
                    )
        return None

    path = _which_tool(tool)
    if path:
        return path

    if runtime_mode == "auto" and runtime.is_windows_host():
        pacman_path = runtime_paths.pacman_tool_path(tool)
        if pacman_path:
            return runtime.pacman_tool_command(pacman_path)

    if should_consider_wsl:
        for executable_name in _tool_executable_names(tool):
            if runtime_paths.wsl_command_available(executable_name):
                return runtime.wsl_tool_command(executable_name)
        if tool in PIXI_TOOL_ENVS:
            for executable_name in _tool_executable_names(tool):
                if runtime_paths.wsl_pixi_tool_available(
                    executable_name, PIXI_TOOL_ENVS[tool]
                ):
                    return runtime.wsl_tool_command(
                        f"{_wsl_pixi_path()} run -e {PIXI_TOOL_ENVS[tool]} {executable_name}"
                    )

    auto_bundled_modes = bundled_modes if runtime_mode == "auto" else ()
    for mode in auto_bundled_modes:
        if runtime_paths.bundled_command_available(mode, tool):
            return runtime.bundled_tool_command(mode, tool)

    host_pixi_command = _host_pixi_tool_command(tool)
    if host_pixi_command:
        return host_pixi_command

    return None


def check_dependencies(tool_list: list[str], jar_dir: str | None = None) -> list[str]:
    """
    Checks if all required tools or JAR files are available in the PATH or Pixi.
    Returns a list of missing tools.
    """
    missing = []
    if jar_dir is None:
        jar_dir = get_jar_dir()

    for tool in tool_list:
        if tool.endswith(".jar"):
            if not Path(jar_dir, tool).exists():
                missing.append(f"{tool} (in {jar_dir})")
        else:
            if get_tool_path(tool) is None:
                missing.append(tool)
    return missing
