import argparse
import logging
import sys
from typing import Any

from wgsextract_cli.core.dependency_checks import check_all_dependencies
from wgsextract_cli.core.pixi_tools import pixi_install_command
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


def _status_text(path: object, *, optional: bool = False, alt_env: bool = False) -> str:
    present = bool(path)
    if _stdout_can_encode("✅", "❌", "⚠️", "🔵"):
        if present and alt_env:
            return "🔵"
        if present:
            return "✅"
        return "⚠️ " if optional else "❌"
    if present and alt_env:
        return "ALT"
    if present:
        return "OK"
    return "WARN" if optional else "MISS"


def _tool_source(tool: dict[str, Any]) -> str:
    alt_env = tool.get("alt_env")
    if alt_env:
        return f" [pixi alt env: {alt_env}]"
    return f" [{tool['runtime']}]" if tool.get("runtime") else ""


def run(args: argparse.Namespace) -> None:
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
            command = pixi_install_command(args.tool)
            hint = f". Run: {command}" if command else ""
            raise WGSExtractError(f"Tool not found: {args.tool}{hint}")

    logging.info("Verifying bioinformatics tool installations...")
    results = check_all_dependencies()

    print("\nMandatory Tools:")
    print("-" * 60)
    for tool in results["mandatory"]:
        status = _status_text(tool["path"], alt_env=bool(tool.get("alt_env")))
        source = _tool_source(tool)
        version = f" ({tool['version']})" if tool["version"] else ""
        print(f"{status} {tool['name']:<20} {source}{version}")

    print("\nOptional Tools:")
    print("-" * 60)
    for tool in results["optional"]:
        status = _status_text(
            tool["path"], optional=True, alt_env=bool(tool.get("alt_env"))
        )
        source = _tool_source(tool)
        version = f" ({tool['version']})" if tool["version"] else ""
        print(f"{status} {tool['name']:<20} {source}{version}")

    print("\n" + "=" * 60)
    missing = [tool["name"] for tool in results["mandatory"] if not tool["path"]]
    if missing:
        raise WGSExtractError(
            "Missing mandatory tool(s): "
            + ", ".join(missing)
            + ". Install them to ensure full functionality."
        )
    logging.info("All mandatory tools verified successfully.")
