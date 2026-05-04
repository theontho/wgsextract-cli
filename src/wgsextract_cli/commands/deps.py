import logging
import os
import subprocess
import sys
from typing import Any

from wgsextract_cli.core import runtime
from wgsextract_cli.core.dependencies import check_all_dependencies
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.utils import WGSExtractError


def _stdout_can_encode(*values: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return True
    try:
        for value in values:
            value.encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


def _status_text(path: object, *, optional: bool = False) -> str:
    present = bool(path)
    if _stdout_can_encode("✅", "❌", "⚠️"):
        if present:
            return "✅"
        return "⚠️ " if optional else "❌"
    if present:
        return "OK"
    return "WARN" if optional else "MISS"


def register(subparsers: Any, base_parser: Any) -> None:
    deps_parser = subparsers.add_parser(
        "deps", parents=[base_parser], help=CLI_HELP["cmd_deps"]
    )
    deps_subparsers = deps_parser.add_subparsers(dest="subcommand", required=True)

    check_parser = deps_subparsers.add_parser(
        "check", parents=[base_parser], help=CLI_HELP["cmd_check-deps"]
    )
    check_parser.add_argument(
        "--tool", help="Check for a specific tool and return exit code 1 if missing."
    )
    check_parser.set_defaults(func=run)

    wsl_parser = deps_subparsers.add_parser(
        "wsl", parents=[base_parser], help="Check or tune the Windows WSL runtime."
    )
    wsl_subparsers = wsl_parser.add_subparsers(dest="wsl_command", required=True)

    wsl_check_parser = wsl_subparsers.add_parser(
        "check", help="Check WSL runtime availability."
    )
    wsl_check_parser.set_defaults(func=run_wsl_check)

    wsl_tune_parser = wsl_subparsers.add_parser(
        "tune",
        help="Write WSL2 resource settings. Uses host-based defaults if omitted.",
    )
    wsl_tune_parser.add_argument(
        "--memory", help="WSL memory limit, such as 24GB. Defaults to 75% of RAM."
    )
    wsl_tune_parser.add_argument(
        "--processors",
        type=int,
        help="Number of CPU processors available to WSL. Defaults to 2/3 of logical CPUs.",
    )
    wsl_tune_parser.add_argument(
        "--swap", help="WSL swap size, such as 16GB. Defaults to 25% of RAM."
    )
    wsl_tune_parser.set_defaults(func=run_wsl_tune)


def run(args: Any) -> None:
    if args.tool:
        from wgsextract_cli.core.dependencies import get_tool_path

        path = get_tool_path(args.tool)
        if path:
            if not args.debug:
                print(path)
            else:
                logging.debug(f"Found {args.tool} at {path}")
            return
        else:
            raise WGSExtractError(f"Tool not found: {args.tool}")

    logging.info("Verifying bioinformatics tool installations...")
    results = check_all_dependencies()

    print("\nMandatory Tools:")
    print("-" * 60)
    all_mandatory_present = True
    for tool in results["mandatory"]:
        status = _status_text(tool["path"])
        source = f" [{tool['runtime']}]" if tool.get("runtime") else ""
        version = f" ({tool['version']})" if tool["version"] else ""
        print(f"{status} {tool['name']:<20} {source}{version}")
        if not tool["path"]:
            all_mandatory_present = False

    print("\nOptional Tools:")
    print("-" * 60)
    for tool in results["optional"]:
        status = _status_text(tool["path"], optional=True)
        source = f" [{tool['runtime']}]" if tool.get("runtime") else ""
        version = f" ({tool['version']})" if tool["version"] else ""
        print(f"{status} {tool['name']:<20} {source}{version}")

    print("\n" + "=" * 60)
    if all_mandatory_present:
        logging.info("All mandatory tools verified successfully.")
    else:
        logging.error(
            "Some mandatory tools are missing. Please install them to ensure full functionality."
        )


def _run_wsl_info(command: str) -> str | None:
    try:
        result = subprocess.run(
            ["wsl", "bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.replace("\x00", "").strip()


def run_wsl_check(args: Any) -> None:
    print("WSL Runtime")
    print("-" * 60)
    print(f"Host platform:       {sys.platform}")
    print(f"Configured runtime:  {runtime.get_tool_runtime_mode()}")

    available = runtime.detect_wsl_available(force=True)
    print(f"WSL available:       {'yes' if available else 'no'}")
    if not available:
        raise WGSExtractError(
            "WSL is not available. Run bootstrap_wsl.ps1 or install WSL with 'wsl --install'."
        )

    distro = _run_wsl_info("printf '%s' \"$(. /etc/os-release && echo $PRETTY_NAME)\"")
    kernel = _run_wsl_info("uname -r")
    pixi = _run_wsl_info("~/.pixi/bin/pixi --version 2>/dev/null || true")

    print(f"WSL distro:          {distro or 'unknown'}")
    print(f"WSL kernel:          {kernel or 'unknown'}")
    print(f"WSL pixi:            {pixi or 'not found at ~/.pixi/bin/pixi'}")
    if runtime.is_windows_host():
        print(f"Current repo in WSL: {runtime.windows_to_wsl_path(os.getcwd())}")

    config_path = runtime.get_wslconfig_path()
    settings = runtime.read_wslconfig_settings(config_path)
    print(f".wslconfig:          {config_path}")
    for key in ["memory", "processors", "swap"]:
        print(f"  {key:<10}       {settings.get(key, 'unset')}")

    print("\nMandatory tools")
    print("-" * 60)
    results = check_all_dependencies()
    missing: list[str] = []
    for tool in results["mandatory"]:
        if tool["name"] == "Python Runtime":
            continue
        status = "yes" if tool["path"] else "no"
        print(f"{tool['name']:<20} {status:<4} {tool['path'] or ''}")
        if not tool["path"]:
            missing.append(str(tool["name"]))

    if missing:
        raise WGSExtractError("Missing WSL-backed tool(s): " + ", ".join(missing))


def run_wsl_tune(args: Any) -> None:
    recommendation = runtime.recommend_wslconfig_settings()
    memory = args.memory or recommendation.memory
    processors = (
        args.processors if args.processors is not None else recommendation.processors
    )
    swap = args.swap or recommendation.swap

    config_path = runtime.write_wslconfig_settings(
        memory=memory,
        processors=processors,
        swap=swap,
    )
    print(f"Updated {config_path}")
    print(
        "WSL defaults use benchmark-backed host ratios: "
        "processors=2/3, memory=3/4, swap=1/4."
    )
    print(
        "Resolved settings: "
        f"memory={memory}, processors={processors}, swap={swap} "
        f"(host: {recommendation.host_processors} CPUs, "
        f"{recommendation.host_memory_gb}GB RAM)"
    )
    print("Run 'wsl --shutdown' or reboot Windows for these settings to take effect.")
