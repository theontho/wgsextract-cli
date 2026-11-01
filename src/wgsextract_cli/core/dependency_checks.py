import json
import logging
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from wgsextract_cli.core.native_support import (
    require_native_tool,
    tool_unsupported_reason,
)
from wgsextract_cli.core.pixi_tools import pixi_install_command
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.windows_arm64 import (
    assert_native_windows_command,
    unsupported_windows_arm64_tool,
)

from .dependencies import (
    check_dependencies,
    get_tool_path,
    get_tool_runtime,
)
from .tool_catalog import (
    OPTIONAL_TOOLS,
    PIXI_TOOL_ENVS,
    optional_dependency_tools,
    required_dependency_tools,
)
from .tool_commands import _tool_command_parts


def _pixi_alt_env(tool: str, path: str | None) -> str | None:
    env = PIXI_TOOL_ENVS.get(tool)
    if not path or not env or env == "default":
        return None
    if get_tool_runtime(path) != "pixi":
        return None
    return env


_VERSION_LIKE_RE = re.compile(r"version|release|\bv\d", re.IGNORECASE)


def _optional_pixi_env(command: list[str]) -> str | None:
    """The environment of a `pixi run ... -e <env> <tool>` command, unless default."""
    if not command or "run" not in command[:2]:
        return None
    if not re.split(r"[\\/]", command[0])[-1].lower().startswith("pixi"):
        return None
    try:
        env = command[command.index("-e") + 1]
    except (ValueError, IndexError):
        return None
    return None if env == "default" else env


def _pixi_env_installed(pixi_run: list[str], env: str) -> bool:
    """Whether `pixi run ... -e env` would find the environment already installed.

    `pixi run --no-install` can't tell: it runs the command without the
    environment when it's missing. Pixi marks a finished install with
    conda-meta/pixi in the environment's prefix, which `pixi info` reports.
    """
    run_at = pixi_run.index("run")
    options = pixi_run[run_at + 1 : pixi_run.index("-e")]
    manifest = [
        arg
        for index, arg in enumerate(options)
        if arg.startswith("--manifest-path")
        or (index > 0 and options[index - 1] == "--manifest-path")
    ]
    try:
        info = subprocess.run(
            [*pixi_run[:run_at], "info", "--json", *manifest],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        environments = json.loads(info.stdout).get("environments_info", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    for environment in environments:
        if environment.get("name") == env and environment.get("prefix"):
            return (Path(environment["prefix"]) / "conda-meta" / "pixi").is_file()
    return False


# Windows' exit codes for a process the system ended (NTSTATUS errors).
_NTSTATUS_CRASHES = {
    0xC0000005: "access violation",
    0xC000001D: "illegal instruction",
    0xC0000094: "integer division by zero",
    0xC00000FD: "stack overflow",
    0xC0000409: "fast fail",
    0xC0000135: "a DLL it needs was not found",
    0xC0000139: "a DLL lacks a function it needs",
    0xC0000142: "a DLL failed to initialize",
    0xC000007B: "not a valid Windows program for this machine",
}


def _crash_description(returncode: int) -> str | None:
    """Describe a death by signal (negative code, or 128+N from a wrapper shell),
    or on Windows by an NTSTATUS error."""
    if returncode >= 0xC0000000:
        name = _NTSTATUS_CRASHES.get(returncode, f"NTSTATUS 0x{returncode:08X}")
        return f"crashed ({name})"
    if sys.platform == "win32":
        return None
    signum = -returncode if returncode < 0 else returncode - 128
    if returncode < 0 or 128 < returncode < 160:
        try:
            return f"crashed ({signal.Signals(signum).name})"
        except ValueError:
            return f"crashed (signal {signum})"
    return None


def _launch_failure(tool: str, returncode: int, output: str) -> str | None:
    """Describe a tool that couldn't start: exit 127 (not found, or the
    loader failed) or glibc's missing-library error."""
    if returncode != 127 and "error while loading shared libraries" not in output:
        return None
    first_line = output.splitlines()[0] if output else None
    return f"Error: {first_line or f'{tool} could not be started (exit 127)'}"


_USAGE_RE = re.compile(r"^\s*usage:", re.IGNORECASE | re.MULTILINE)

# A version probe's time limit. A tool's first start on a freshly installed
# Windows PC can take far longer than later ones (Python compiles its bytecode,
# Defender scans every new file): Yleaf's `--help` took 24 s there, then 1 s.
VERSION_PROBE_TIMEOUT = 60


def _version_output(stdout: str, stderr: str) -> str:
    """Return the version text, without blank and debug lines."""
    lines = []
    for stream_text in (stdout, stderr):
        for line in stream_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("DEBUG:"):
                continue
            lines.append(stripped)
    return "\n".join(lines)


# Tools whose version isn't printed by --version (bwa-mem2 prints its usage).
VERSION_ARGS = {"yleaf": "--help", "bwa-mem2": "version"}


def get_tool_version(tool: str) -> str | None:
    """Attempt to get the version of a tool by running it."""
    if reason := (
        unsupported_windows_arm64_tool(tool) or tool_unsupported_reason(tool)
    ):
        return f"Error: {reason}"
    # Use the path/command from get_tool_path to handle pixi correctly
    cmd_base = get_tool_path(tool)
    if not cmd_base:
        return None

    full_cmd = _tool_command_parts(cmd_base)
    try:
        assert_native_windows_command(full_cmd)
    except (OSError, ValueError) as error:
        return f"Error: {error}"
    optional_env = _optional_pixi_env(full_cmd)
    if optional_env:
        # A version probe must never download an optional environment.
        if not _pixi_env_installed(full_cmd, optional_env):
            command = pixi_install_command(tool)
            hint = (
                f" Run: {command}"
                if command
                else " See the installation guide for host support."
            )
            return f"Error: Pixi env '{optional_env}' is not installed.{hint}"
        if "--no-install" not in full_cmd:
            full_cmd.insert(full_cmd.index("run") + 1, "--no-install")

    try:
        # 1. Try --version first (standard)
        version_arg = VERSION_ARGS.get(tool, "--version")
        res = subprocess.run(
            full_cmd + [version_arg],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=VERSION_PROBE_TIMEOUT,
        )
        if crashed := _crash_description(res.returncode):
            return f"Error: {tool} {crashed}"
        output = _version_output(res.stdout, res.stderr)
        if failure := _launch_failure(tool, res.returncode, output):
            return failure

        if res.returncode == 0 and output and not output.startswith("[main]"):
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            if tool == "yleaf":
                for line in lines:
                    if line.startswith("Yleaf:") and " v" in line:
                        return line
            if lines:
                return lines[0]

        # 2. Fallback for tools that use bare command or --help
        res = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=VERSION_PROBE_TIMEOUT,
        )
        if crashed := _crash_description(res.returncode):
            return f"Error: {tool} {crashed}"
        output = _version_output(res.stdout, res.stderr)

        # Check for dyld/library errors
        failure_keywords = [
            "Library not loaded",
            "not found",
            "illegal instruction",
            "segmentation fault",
            "bad interpreter",
        ]
        lowered = output.lower()
        failures = [kw for kw in failure_keywords if kw.lower() in lowered]
        # "not found" alone is excused in help text, or when it isn't about the tool.
        excused = failures == ["not found"] and (
            tool.lower() not in lowered or _USAGE_RE.search(output)
        )
        if failures and not excused:
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
            if _VERSION_LIKE_RE.search(line) and len(line.strip()) < 60:
                return line.strip()

        return "Available"
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        return f"Error: {str(e)}"


def verify_dependencies(
    tool_list: list[str], optional_list: list[str] | None = None
) -> None:
    """
    Checks if all required tools or JAR files are available.
    Exits gracefully if a tool is missing or version is too old.
    """
    for tool in tool_list:
        if reason := unsupported_windows_arm64_tool(tool):
            raise WGSExtractError(reason)
    if optional_list is None:
        optional_list = OPTIONAL_TOOLS

    for tool in tool_list:
        require_native_tool(tool)
    missing = check_dependencies(tool_list)

    if missing:
        is_optional = all(tool in optional_list for tool in missing)
        if is_optional:
            logging.error("Required optional tool(s) missing for this feature:")
        else:
            logging.error("Missing required core tools or JAR files.")
        for t in missing:
            logging.error(f" - {t}")

        if is_optional:
            message = f"Missing required optional tool(s): {', '.join(missing)}"
            commands = sorted(
                {command for tool in missing if (command := pixi_install_command(tool))}
            )
            if commands:
                message += ". Run: " + "; ".join(commands)
            if sys.platform == "win32" and "gatk" in missing:
                repair = (
                    "Re-run install_windows.bat or run "
                    "'pixi install --locked -e gatk' to restore GATK 4.6.2.0 "
                    "and its native GKL sidecar."
                )
                logging.info(repair)
                message += f". {repair}"
            else:
                logging.info(
                    "Please install the missing tools using your system package manager "
                    "(e.g., brew, apt, conda) or follow the project installation guide."
                )
            raise WGSExtractError(message)
        logging.error(
            "Please ensure all mandatory tools are installed and in your PATH."
        )
        raise WGSExtractError(f"Missing required core tool(s): {', '.join(missing)}")

    # Version Validation for critical tools
    for tool in ["bcftools", "samtools"]:
        if tool in tool_list and tool not in missing:
            version_str = get_tool_version(tool)
            if not version_str:
                continue
            if version_str.startswith("Error:"):
                # Found but unable to run (a crash, a missing library): as
                # good as missing, as `deps check` reports it.
                logging.error(f"{tool} at {get_tool_path(tool)} does not run.")
                raise WGSExtractError(
                    f"{tool} does not run: {version_str.removeprefix('Error: ')}"
                )

            # Handle version strings like "bcftools 1.12" or "Version: 0.1.19"

            match = re.search(r"(\d+)\.(\d+)", version_str)
            if match:
                major = int(match.group(1))
                if major < 1:
                    logging.error(f"This tool requires {tool} version 1.0 or newer.")
                    logging.info(
                        f"Your current path for {tool} is: {get_tool_path(tool)}"
                    )
                    logging.info("Please update your conda environment or system path.")
                    raise WGSExtractError(f"{tool} version {version_str} is too old.")


def _missing_tool_hint(tool: str) -> str | None:
    unsupported = unsupported_windows_arm64_tool(tool) or tool_unsupported_reason(tool)
    if unsupported:
        return f"Error: {unsupported}"
    command = pixi_install_command(tool)
    return f"Run: {command}" if command else None


def check_all_dependencies(
    mandatory: list[str] | None = None,
    optional: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Performs a simple dependency check and returns results."""
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
            "runtime": get_tool_runtime(sys.executable),
            "version": f"Python {py_version} (Required >= 3.10)",
        }
    )

    for tool in mandatory:
        if tool == "python3":
            continue  # Already checked above as Runtime
        path = get_tool_path(tool)
        version = get_tool_version(tool) if path else _missing_tool_hint(tool)
        is_broken = version and version.startswith("Error:")
        present_path = path if not is_broken else None

        results["mandatory"].append(
            {
                "name": tool,
                "path": present_path,
                "runtime": get_tool_runtime(present_path),
                "version": version,
                "alt_env": _pixi_alt_env(tool, present_path),
            }
        )

    for tool in optional:
        path = get_tool_path(tool)
        version = get_tool_version(tool) if path else _missing_tool_hint(tool)

        is_broken = version and version.startswith("Error:")
        present_path = path if not is_broken else None

        results["optional"].append(
            {
                "name": tool,
                "path": present_path,
                "runtime": get_tool_runtime(present_path),
                "version": version,
                "alt_env": _pixi_alt_env(tool, present_path),
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
