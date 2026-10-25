import shutil
from functools import lru_cache
from pathlib import Path

from wgsextract_cli.core import runtime
from wgsextract_cli.core.paths import expand_path
from wgsextract_cli.core.pixi_tools import (
    _host_pixi_tool_command,
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
    """Return where a resolved dependency comes from: pixi, native or missing."""
    if path is None:
        return "missing"
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


def _which_tool(tool: str) -> str | None:
    for executable_name in _tool_executable_names(tool):
        path = shutil.which(executable_name)
        if path:
            return path
    return None


def get_tool_path(tool: str) -> str | None:
    """Return how to invoke a tool (path or wrapper command), or None if missing.

    A tool on PATH (inside `pixi run`, the default environment's bin, or
    Library\\bin on Windows) comes first, then the tool's own Pixi environment.
    Results are cached per process (resolution may spawn ``pixi run`` probes);
    ``clear_tool_caches`` drops them, and config reloads call it.
    """
    return _resolve_tool_path(tool)


def clear_tool_caches() -> None:
    _resolve_tool_path.cache_clear()


@lru_cache(maxsize=256)
def _resolve_tool_path(tool: str) -> str | None:
    return _which_tool(tool) or _host_pixi_tool_command(tool)


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
