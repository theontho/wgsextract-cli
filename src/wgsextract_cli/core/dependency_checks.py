import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any

from wgsextract_cli.core import (
    runtime_wrappers,
)
from wgsextract_cli.core.utils import WGSExtractError

from .dependencies import (
    OPTIONAL_TOOLS,
    _tool_command_parts,
    _version_output,
    check_dependencies,
    get_jar_dir,
    get_tool_path,
    get_tool_runtime,
    optional_dependency_tools,
    required_dependency_tools,
)


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
            runtime_wrappers.wrap_command(full_cmd + ["--version"]),
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
            runtime_wrappers.wrap_command(full_cmd),
            capture_output=True,
            text=True,
            timeout=10,
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
            raise WGSExtractError(
                f"Missing required optional tool(s): {', '.join(missing)}"
            )
        elif not is_windows:
            logging.error(
                "\nPlease ensure all mandatory tools are installed and in your PATH."
            )
            raise WGSExtractError(
                f"Missing required core tool(s): {', '.join(missing)}"
            )
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
                        raise WGSExtractError(
                            f"{tool} version {version_str} is too old."
                        )


def get_jar_path(jar_name: str) -> str | None:
    """Returns absolute path to a JAR file in jartools/."""
    path = os.path.join(get_jar_dir(), jar_name)
    if os.path.exists(path):
        return path
    return None


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
