import ctypes
import os
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from wgsextract_cli.core import (
    runtime,
    runtime_paths,
)

MANDATORY_TOOLS = [
    "samtools",
    "bcftools",
    "tabix",
    "bgzip",
    "bwa",
    "python3",
    "gzip",
    "tar",
]


OPTIONAL_TOOLS = [
    "minimap2",
    "pbmm2",
    "pbsv",
    "sniffles",
    "sambamba",
    "samblaster",
    "fastp",
    "fastqc",
    "delly",
    "freebayes",
    "vep",
    "gatk",
    "run_deepvariant",
    "dv_call_variants.py",
    "curl",
    "java",
    "yleaf",
    "haplogrep",
    "htsfile",
]


PIXI_TOOL_ENVS = {
    "gzip": "default",
    "tar": "default",
    "curl": "default",
    "fastp": "default",
    "fastqc": "default",
    "yleaf": "default",
    "vep": "vep",
    "run_deepvariant": "deepvariant",
    "haplogrep": "default",
    "gatk": "default",
    "delly": "default",
    "freebayes": "default",
    "samtools": "default",
    "bcftools": "default",
    "bgzip": "default",
    "tabix": "default",
    "java": "default",
    "sambamba": "default",
    "samblaster": "default",
    "bwa": "default",
    "minimap2": "default",
    "pbmm2": "pacbio",
    "pbsv": "pacbio",
    "sniffles": "pacbio",
    "htsfile": "default",
}


TOOL_EXECUTABLE_ALIASES = {
    "yleaf": ("yleaf", "Yleaf"),
}


WINDOWS_PIXI_SCRIPT_LAUNCHERS = {
    "fastqc": ("perl",),
    "gatk": ("python",),
}


IGNORED_VERSION_OUTPUT_PREFIXES = ("wsl: Failed to mount ",)


@lru_cache(maxsize=1)
def _wsl_home_dir() -> str | None:
    if not runtime.is_windows_host():
        return None
    try:
        result = subprocess.run(
            ["wsl", "bash", "-lc", 'printf %s "$HOME"'],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    home = result.stdout.strip()
    return home if home else None


def _wsl_pixi_path() -> str:
    home = _wsl_home_dir()
    if not home:
        username = os.environ.get("USERNAME")
        home = f"/home/{username}" if username else "~"
    return f"{home}/.pixi/bin/pixi"


def required_dependency_tools(include_python: bool = True) -> list[str]:
    """Return the centrally-defined required dependency tools."""
    if include_python:
        return list(MANDATORY_TOOLS)
    return [tool for tool in MANDATORY_TOOLS if tool != "python3"]


def optional_dependency_tools() -> list[str]:
    """Return the centrally-defined optional dependency tools."""
    return list(OPTIONAL_TOOLS)


def _version_output(stdout: str, stderr: str) -> str:
    """Return useful version text, filtering host-runtime noise."""
    lines = []
    for stream_text in (stdout, stderr):
        for line in stream_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(IGNORED_VERSION_OUTPUT_PREFIXES):
                continue
            if stripped.startswith("DEBUG:"):
                continue
            lines.append(stripped)
    return "\n".join(lines)


def _tool_command_parts(cmd_base: str) -> list[str]:
    """Split wrapper commands without corrupting native executable paths."""
    normalized_cmd = os.path.expanduser(cmd_base)
    if (
        runtime.is_wsl_tool_command(normalized_cmd)
        or runtime.is_bundled_tool_command(normalized_cmd)
        or runtime.is_pacman_tool_command(normalized_cmd)
    ):
        return [normalized_cmd]
    if os.path.exists(normalized_cmd):
        return [normalized_cmd]
    if runtime.is_windows_host():
        return _windows_command_line_split(normalized_cmd)
    return shlex.split(normalized_cmd)


def _windows_command_line_split(command_line: str) -> list[str]:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return shlex.split(command_line, posix=False)

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


def is_pixi_tool_command(path: str) -> bool:
    """Return whether a resolved command is explicitly launched through Pixi."""
    try:
        command = shlex.split(
            os.path.expanduser(path), posix=not runtime.is_windows_host()
        )
    except ValueError:
        return False
    if not command:
        return False
    executable = os.path.basename(command[0]).lower()
    return executable in {"pixi", "pixi.exe"} and "run" in command[1:]


def is_pixi_tool_path(path: str) -> bool:
    """Return whether a resolved executable lives inside the active Pixi env."""
    expanded_path = os.path.abspath(os.path.expanduser(path))
    pixi_prefix = os.environ.get("PIXI_PREFIX")
    if pixi_prefix and _is_relative_to(expanded_path, pixi_prefix):
        return True
    conda_prefix = os.environ.get("CONDA_PREFIX")
    return bool(
        conda_prefix
        and _is_pixi_environment_active()
        and _is_relative_to(expanded_path, conda_prefix)
    )


def _is_pixi_environment_active() -> bool:
    pixi_env_vars = (
        "PIXI_EXE",
        "PIXI_IN_SHELL",
        "PIXI_PROJECT_MANIFEST",
        "PIXI_PROJECT_NAME",
        "PIXI_PROJECT_ROOT",
        "PIXI_PROJECT_VERSION",
    )
    return any(os.environ.get(env_var) for env_var in pixi_env_vars)


def _is_relative_to(path: str, parent: str) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def get_repo_root() -> str:
    """
    Attempts to find the repository root by looking for common markers.
    Defaults to 4 levels up from this file if no markers are found.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Check for pyproject.toml or .git in parents
    probe = current_dir
    while probe != os.path.dirname(probe):  # stop at filesystem root
        if os.path.exists(os.path.join(probe, "pyproject.toml")) or os.path.exists(
            os.path.join(probe, ".git")
        ):
            # If we found it in 'cli/', the real repo root is one level up
            return probe
        probe = os.path.dirname(probe)

    # Fallback to hardcoded relative path if markers not found
    return os.path.abspath(os.path.join(current_dir, "../../../.."))


def get_jar_dir() -> str:
    """Returns the directory where JAR tools are expected to live."""
    # Allow override via environment variable or config
    from wgsextract_cli.core.config import settings

    env_path = settings.get("jar_directory")
    if env_path and isinstance(env_path, str):
        expanded_path = os.path.expanduser(str(env_path))
        if os.path.isdir(expanded_path):
            return expanded_path

    path: str = os.path.join(str(get_repo_root()), "jartools")
    return path


def _candidate_bundled_runtime_modes(runtime_mode: str) -> tuple[str, ...]:
    if runtime_mode in runtime.BUNDLED_RUNTIME_MODES:
        return (runtime_mode,)
    if runtime_mode == "auto" and runtime.is_windows_host():
        return ("cygwin", "msys2")
    return ()


def _resolve_pixi_command() -> str | None:
    pixi_cmd = shutil.which("pixi")
    if pixi_cmd:
        return pixi_cmd

    for path in [
        "/opt/homebrew/bin/pixi",
        "/usr/local/bin/pixi",
        "~/.pixi/bin/pixi",
        "~/.pixi/bin/pixi.exe",
    ]:
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
    return None


def _pixi_run_command(pixi_cmd: str, env: str, args: list[str]) -> str:
    command = [pixi_cmd, "run", "-e", env, *args]
    if runtime.is_windows_host():
        return subprocess.list2cmdline(command)
    return " ".join(shlex.quote(part) for part in command)


def _tool_executable_names(tool: str) -> tuple[str, ...]:
    return TOOL_EXECUTABLE_ALIASES.get(tool, (tool,))


def _which_tool(tool: str) -> str | None:
    for executable_name in _tool_executable_names(tool):
        path = shutil.which(executable_name)
        if path:
            return path
    return None


def _host_pixi_tool_command(tool: str) -> str | None:
    if tool not in PIXI_TOOL_ENVS:
        return None

    env = PIXI_TOOL_ENVS[tool]
    pixi_cmd = _resolve_pixi_command()
    if not pixi_cmd:
        return None

    probe = r"""
import shutil
import sys
import os
from pathlib import Path

tool_names = sys.argv[1:]
for tool in tool_names:
    path = shutil.which(tool)
    if path:
        print("direct-name:" + tool)
        raise SystemExit(0)

prefix = Path(sys.prefix)
if "haplogrep" in tool_names:
    jar = prefix / "bin" / "haplogrep.jar"
    if jar.exists():
        print("haplogrep:" + str(jar.resolve()))
        raise SystemExit(0)

for tool in tool_names:
    script = prefix / "bin" / tool
    if os.path.lexists(script):
        print("script:" + str(script.resolve()))
        raise SystemExit(0)

raise SystemExit(1)
"""
    executable_names = _tool_executable_names(tool)
    try:
        result = subprocess.run(
            [pixi_cmd, "run", "-e", env, "python", "-c", probe, *executable_names],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip().splitlines()
    if not output:
        return None
    kind, separator, value = output[-1].partition(":")
    if not separator or not value:
        return None

    if kind == "direct":
        return _pixi_run_command(pixi_cmd, env, [tool])
    if kind == "direct-name":
        return _pixi_run_command(pixi_cmd, env, [value])
    if kind == "haplogrep":
        return _pixi_run_command(pixi_cmd, env, ["java", "-jar", value])
    if kind == "script" and tool in WINDOWS_PIXI_SCRIPT_LAUNCHERS:
        return _pixi_run_command(
            pixi_cmd, env, [*WINDOWS_PIXI_SCRIPT_LAUNCHERS[tool], value]
        )
    if kind == "script" and not runtime.is_windows_host():
        return _pixi_run_command(pixi_cmd, env, [value])
    return None


def get_tool_path(tool: str) -> str | None:
    """Returns the path to a tool if it exists in the system PATH or pixi environments."""
    runtime_mode = runtime.get_tool_runtime_mode()
    should_consider_wsl = runtime.should_consider_wsl()
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
            if not os.path.exists(os.path.join(jar_dir, tool)):
                missing.append(f"{tool} (in {jar_dir})")
        else:
            if get_tool_path(tool) is None:
                missing.append(tool)
    return missing
