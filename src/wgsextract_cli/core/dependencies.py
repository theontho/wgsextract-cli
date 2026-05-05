import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from functools import lru_cache
from typing import Any

from wgsextract_cli.core import runtime

# Define mandatory and optional tools
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
    "yleaf": "yleaf",
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
    "htsfile": "default",
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
    except Exception:
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
    if (
        runtime.is_wsl_tool_command(cmd_base)
        or runtime.is_bundled_tool_command(cmd_base)
        or runtime.is_pacman_tool_command(cmd_base)
    ):
        return [cmd_base]
    if os.path.exists(cmd_base):
        return [cmd_base]
    return shlex.split(cmd_base)


def get_tool_runtime(path: str | None) -> str:
    """Return the runtime used for a resolved dependency path."""
    if path is None:
        return "missing"
    if runtime.is_wsl_tool_command(path):
        return "wsl"
    bundled_mode = runtime.bundled_tool_command_mode(path)
    if bundled_mode:
        return bundled_mode
    if runtime.is_pacman_tool_command(path) or runtime.is_pacman_tool_path(path):
        return "pacman"
    return "native"


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
    if env_path and isinstance(env_path, str) and os.path.isdir(env_path):
        return str(env_path)

    path: str = os.path.join(str(get_repo_root()), "jartools")
    return path


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


def verify_dependencies(
    tool_list: list[str], optional_list: list[str] | None = None
) -> None:
    """
    Checks if all required tools or JAR files are available.
    Exits gracefully if a tool is missing or version is too old.
    """
    if optional_list is None:
        optional_list = OPTIONAL_TOOLS

    missing = check_dependencies(tool_list)

    if missing:
        is_optional = all(tool in optional_list for tool in missing)
        is_windows = sys.platform == "win32"

        if is_optional:
            logging.error("Required optional tool(s) missing for this feature:")
        else:
            if is_windows:
                logging.warning(
                    "Warning: Missing required core tools on Windows. Some features may not work."
                )
                logging.warning(
                    "Use 'wgsextract deps wsl check' for WSL, or "
                    "'wgsextract deps cygwin setup' / 'wgsextract deps msys2 setup' "
                    "for bundled Windows runtimes, or 'wgsextract deps pacman check' "
                    "for MSYS2/UCRT64 pacman tools."
                )
            else:
                logging.error("Fatal Error: Missing required core tools or JAR files.")

        for t in missing:
            if is_windows and not is_optional:
                logging.warning(f" - {t} (Missing)")
            else:
                logging.error(f" - {t}")

        if is_optional:
            logging.info(
                "\nPlease install the missing tools using your system package manager "
                "(e.g., brew, apt, conda) or follow the project installation guide."
            )
            sys.exit(1)
        elif not is_windows:
            logging.error(
                "\nPlease ensure all mandatory tools are installed and in your PATH."
            )
            sys.exit(1)
        else:
            logging.warning(
                "\nProceeding anyway, but expect failures in bio-tool commands "
                "unless WSL tools are configured."
            )

    # Version Validation for critical tools
    for tool in ["bcftools", "samtools"]:
        if tool in tool_list:
            version_str = get_tool_version(tool)
            if not version_str:
                continue

            # Handle version strings like "bcftools 1.12" or "Version: 0.1.19"

            match = re.search(r"(\d+)\.(\d+)", version_str)
            if match:
                major = int(match.group(1))
                if major < 1:
                    if sys.platform == "win32":
                        logging.warning(
                            f"Warning: {tool} version {version_str} is too old or unsupported on Windows."
                        )
                    else:
                        logging.error(
                            f"Fatal Error: {tool} version {version_str} is too old."
                        )
                        logging.error(
                            f"This tool requires {tool} version 1.0 or newer."
                        )
                        logging.info(
                            f"\nYour current path for {tool} is: {shutil.which(tool)}"
                        )
                        logging.info(
                            "Please update your conda environment or system path."
                        )
                        sys.exit(1)


def get_jar_path(jar_name: str) -> str | None:
    """Returns absolute path to a JAR file in jartools/."""
    path = os.path.join(get_jar_dir(), jar_name)
    if os.path.exists(path):
        return path
    return None


def get_tool_version(tool: str) -> str | None:
    """Attempt to get the version of a tool by running it."""
    # Use the path/command from get_tool_path to handle pixi correctly
    cmd_base = get_tool_path(tool)
    if not cmd_base:
        return None

    full_cmd = _tool_command_parts(cmd_base)

    try:
        # 1. Try --version first (standard)
        res = subprocess.run(
            runtime.wrap_command(full_cmd + ["--version"]),
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = _version_output(res.stdout, res.stderr)

        if res.returncode == 0 and output and not output.startswith("[main]"):
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            if lines:
                return lines[0]

        # 2. Fallback for tools that use bare command or --help
        res = subprocess.run(
            runtime.wrap_command(full_cmd), capture_output=True, text=True, timeout=10
        )
        output = _version_output(res.stdout, res.stderr)

        # Check for dyld/library errors
        failure_keywords = [
            "Library not loaded",
            "not found",
            "illegal instruction",
            "segmentation fault",
            "bad interpreter",
        ]
        if any(kw.lower() in output.lower() for kw in failure_keywords):
            if "not found" in output.lower() and tool.lower() not in output.lower():
                pass
            else:
                return f"Error: {output.splitlines()[0]}"

        # Search for "Version:" or similar in the output
        for line in output.splitlines():
            line_l = line.lower()
            if "version:" in line_l:
                return line.strip()
            if tool.lower() in line_l and any(c.isdigit() for c in line):
                if len(line.strip()) < 50:
                    return line.strip()

        # Generalized fallback for tools like VEP or others with multi-line help
        if not output:
            return "Available"

        # Check first 5 lines for anything that looks like a version string
        for line in output.splitlines()[:5]:
            if any(word in line.lower() for word in ["version", "release", "v[0-9]"]):
                if len(line.strip()) < 60:
                    return line.strip()

        return "Available"
    except Exception as e:
        return f"Error: {str(e)}"


def get_tool_path(tool: str) -> str | None:
    """Returns the path to a tool if it exists in the system PATH or pixi environments."""
    runtime_mode = runtime.get_tool_runtime_mode()
    should_consider_wsl = runtime.should_consider_wsl()
    prefer_wsl = runtime_mode == "wsl"
    bundled_modes = _candidate_bundled_runtime_modes(runtime_mode)

    if runtime_mode == "pacman":
        pacman_path = runtime.pacman_tool_path(tool)
        if pacman_path:
            return runtime.pacman_tool_command(pacman_path)

    explicit_bundled_modes = (
        bundled_modes if runtime_mode in runtime.BUNDLED_RUNTIME_MODES else ()
    )
    for mode in explicit_bundled_modes:
        if runtime.bundled_command_available(mode, tool):
            return runtime.bundled_tool_command(mode, tool)

    if should_consider_wsl and prefer_wsl and runtime.wsl_command_available(tool):
        return runtime.wsl_tool_command(tool)

    path = shutil.which(tool)
    if path:
        return path

    if runtime_mode == "auto" and runtime.is_windows_host():
        pacman_path = runtime.pacman_tool_path(tool)
        if pacman_path:
            return runtime.pacman_tool_command(pacman_path)

    if should_consider_wsl:
        if runtime.wsl_command_available(tool):
            return runtime.wsl_tool_command(tool)
        if tool in PIXI_TOOL_ENVS and runtime.wsl_pixi_tool_available(
            tool, PIXI_TOOL_ENVS[tool]
        ):
            return runtime.wsl_tool_command(
                f"{_wsl_pixi_path()} run -e {PIXI_TOOL_ENVS[tool]} {tool}"
            )

    auto_bundled_modes = bundled_modes if runtime_mode == "auto" else ()
    for mode in auto_bundled_modes:
        if runtime.bundled_command_available(mode, tool):
            return runtime.bundled_tool_command(mode, tool)

    # Check for pixi sub-environments
    if tool in PIXI_TOOL_ENVS:
        env = PIXI_TOOL_ENVS[tool]

        # Resolve 'pixi' command
        pixi_cmd = shutil.which("pixi")
        if not pixi_cmd:
            # Try some common locations
            for p in [
                "/opt/homebrew/bin/pixi",
                "/usr/local/bin/pixi",
                "~/.pixi/bin/pixi",
            ]:
                expanded = os.path.expanduser(p)
                if os.path.exists(expanded):
                    pixi_cmd = expanded
                    break

        if pixi_cmd:
            try:
                # Use absolute path to pixi to be safe
                res = subprocess.run(
                    [pixi_cmd, "run", "-e", env, "which", tool],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if res.returncode == 0:
                    return f"{pixi_cmd} run -e {env} {tool}"
            except Exception:
                pass

    return None


def _candidate_bundled_runtime_modes(runtime_mode: str) -> tuple[str, ...]:
    if runtime_mode in runtime.BUNDLED_RUNTIME_MODES:
        return (runtime_mode,)
    if runtime_mode == "auto" and runtime.is_windows_host():
        return ("cygwin", "msys2")
    return ()


def check_all_dependencies(
    mandatory: list[str] | None = None, optional: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """
    Performs a simple dependency check and returns results.
    """
    if mandatory is None:
        mandatory = required_dependency_tools()
    if optional is None:
        optional = optional_dependency_tools()

    results: dict[str, list[dict[str, Any]]] = {"mandatory": [], "optional": []}

    # Python Version Check
    py_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    results["mandatory"].append(
        {
            "name": "Python Runtime",
            "path": sys.executable,
            "runtime": "native",
            "version": f"Python {py_version} (Required >= 3.10)",
        }
    )

    for tool in mandatory:
        if tool == "python3":
            continue  # Already checked above as Runtime
        path = get_tool_path(tool)
        version = get_tool_version(tool) if path else None
        is_broken = version and version.startswith("Error:")

        results["mandatory"].append(
            {
                "name": tool,
                "path": path if not is_broken else None,
                "runtime": get_tool_runtime(path if not is_broken else None),
                "version": version,
            }
        )

    for tool in optional:
        path = get_tool_path(tool)
        version = get_tool_version(tool) if path else None

        display_name = tool
        if tool == "run_deepvariant":
            display_name = f"{tool} (Wrapper)"
        elif tool == "dv_call_variants.py":
            display_name = f"{tool} (Bioconda)"

        is_broken = version and version.startswith("Error:")

        results["optional"].append(
            {
                "name": display_name,
                "path": path if not is_broken else None,
                "runtime": get_tool_runtime(path if not is_broken else None),
                "version": version,
            }
        )

    return results


def log_dependency_info(tool_list: list[str]) -> None:
    """Logs the path and version for a list of tools for diagnostic purposes."""
    for tool in tool_list:
        path = get_tool_path(tool)
        if path:
            version = get_tool_version(tool)
            logging.debug(f"Dependency: {tool} -> {path} ({version})")
        else:
            logging.debug(f"Dependency: {tool} -> NOT FOUND")
