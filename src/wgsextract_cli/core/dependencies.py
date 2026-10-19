import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
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
    "dv_make_examples.py": "deepvariant",
    "dv_call_variants.py": "deepvariant",
    "dv_postprocess_variants.py": "deepvariant",
    "haplogrep": "default",
    "gatk": "gatk",
    "delly": "default",
    "freebayes": "freebayes",
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


# Optional tools the CLI only uses on some platforms (sys.platform prefixes).
PLATFORM_ONLY_TOOLS = {"sambamba": ("linux",)}


def optional_dependency_tools() -> list[str]:
    """Return the optional dependency tools that apply to this platform."""
    return [
        tool
        for tool in OPTIONAL_TOOLS
        if sys.platform.startswith(PLATFORM_ONLY_TOOLS.get(tool, ("",)))
    ]


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
    # resolve() can fail on broken Windows paths or symlink loops.
    try:
        return Path(path).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, RuntimeError):
        return False


def get_jar_dir() -> str:
    """Returns the directory where JAR tools are expected to live."""
    from wgsextract_cli.core.config import settings

    configured = settings.get("jar_directory")
    if configured and isinstance(configured, str):
        expanded_path = os.path.expanduser(configured)
        if os.path.isdir(expanded_path):
            return expanded_path

    return str(runtime.repo_root() / "jartools")


def _candidate_bundled_runtime_modes(runtime_mode: str) -> tuple[str, ...]:
    if runtime_mode in runtime.BUNDLED_RUNTIME_MODES:
        return (runtime_mode,)
    if runtime_mode == "auto" and runtime.is_windows_host():
        return ("cygwin", "msys2")
    return ()


def _resolve_pixi_command() -> str | None:
    # The pixi running this process (it exports PIXI_EXE into its environments),
    # then the install's own embedded pixi, before anything on PATH: a system or
    # Homebrew pixi may be a different version with different global config.
    candidates = [os.environ.get("PIXI_EXE")]
    root = runtime.source_checkout_root()
    # Only an install.sh install has its own pixi beside app/ (marked by its
    # manifest); next to a plain checkout that path is the user's ~/.pixi.
    if root is not None and (root.parent / "install-manifest.json").is_file():
        exe = "pixi.exe" if runtime.is_windows_host() else "pixi"
        candidates.append(str(root.parent / ".pixi" / "bin" / exe))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate

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


def _pixi_manifest_args() -> list[str]:
    """Point pixi at this app's manifest; the CLI may run from any directory."""
    root = runtime.source_checkout_root()
    if root and (root / "pixi.toml").is_file():
        return ["--manifest-path", str(root / "pixi.toml")]
    return []


# Conda package that provides a tool in its Pixi environment, when the names differ.
PIXI_TOOL_PACKAGES = {
    "gatk": "gatk4",
    "vep": "ensembl-vep",
    "run_deepvariant": "deepvariant",
    "dv_make_examples.py": "deepvariant",
    "dv_call_variants.py": "deepvariant",
    "dv_postprocess_variants.py": "deepvariant",
}


def _locked_package_available(pixi_cmd: str, env: str, tool: str) -> bool:
    """Whether the lockfile gives this platform's `env` the package providing `tool`.

    Reads the lockfile only, so nothing is downloaded just to answer `deps check`.
    """
    try:
        result = subprocess.run(
            [pixi_cmd, "list", *_pixi_manifest_args(), "-e", env, "--no-install"]
            + ["--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        packages = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    package = PIXI_TOOL_PACKAGES.get(tool, tool)
    return any(entry.get("name") == package for entry in packages)


def _pixi_run_command(pixi_cmd: str, env: str, args: list[str]) -> str:
    command = [pixi_cmd, "run", *_pixi_manifest_args(), "-e", env, *args]
    if runtime.is_windows_host():
        return subprocess.list2cmdline(command)
    return shlex.join(command)


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
import logging
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
            [
                pixi_cmd,
                "run",
                *_pixi_manifest_args(),
                # Never install or update an environment just to probe it.
                "--no-install",
                "-e",
                env,
                "python",
                "-c",
                probe,
                *executable_names,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        # An optional environment that isn't installed yet: `pixi run` installs it
        # (with visible progress) the first time the tool is actually used.
        if env != "default" and _locked_package_available(pixi_cmd, env, tool):
            return _pixi_run_command(pixi_cmd, env, [executable_names[0]])
        return None

    output = result.stdout.strip().splitlines()
    if not output:
        return None
    kind, separator, value = output[-1].partition(":")
    if not separator or not value:
        return None

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
            if not os.path.exists(os.path.join(jar_dir, tool)):
                missing.append(f"{tool} (in {jar_dir})")
        else:
            if get_tool_path(tool) is None:
                missing.append(tool)
    return missing
