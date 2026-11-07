import logging
import sys
from typing import Any

from wgsextract_cli.api.deps import DepsCheckParams
from wgsextract_cli.core.dependency_checks import check_all_dependencies
from wgsextract_cli.core.native_support import require_native_tool
from wgsextract_cli.core.pixi_tools import pixi_install_command
from wgsextract_cli.core.results import current_result, human_output
from wgsextract_cli.core.utils import MissingDependencyError


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


def _row(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        key: tool.get(key) for key in ("name", "path", "runtime", "version", "alt_env")
    }


def run(params: DepsCheckParams, *, debug: bool = False) -> None:
    """``deps check``. ``debug`` (the CLI's --debug) logs one tool's path
    instead of printing it."""
    result = current_result()
    if params.tool:
        require_native_tool(params.tool)
        from wgsextract_cli.core.dependencies import get_tool_path

        path = get_tool_path(params.tool)
        result.set_details({"tool": params.tool, "path": path})
        if path:
            if not debug:
                human_output(path)
            else:
                logging.debug(f"Found {params.tool} at {path}")
            return
        else:
            command = pixi_install_command(params.tool)
            hint = f". Run: {command}" if command else ""
            raise MissingDependencyError(f"Tool not found: {params.tool}{hint}")

    logging.info("Verifying bioinformatics tool installations...")
    results = check_all_dependencies()
    result.set_details(
        {group: [_row(tool) for tool in results[group]] for group in results}
    )

    human_output("\nMandatory Tools:")
    human_output("-" * 60)
    for tool in results["mandatory"]:
        status = _status_text(tool["path"], alt_env=bool(tool.get("alt_env")))
        source = _tool_source(tool)
        version = f" ({tool['version']})" if tool["version"] else ""
        human_output(f"{status} {tool['name']:<20} {source}{version}")

    human_output("\nOptional Tools:")
    human_output("-" * 60)
    for tool in results["optional"]:
        status = _status_text(
            tool["path"], optional=True, alt_env=bool(tool.get("alt_env"))
        )
        source = _tool_source(tool)
        version = f" ({tool['version']})" if tool["version"] else ""
        human_output(f"{status} {tool['name']:<20} {source}{version}")

    human_output("\n" + "=" * 60)
    missing = [tool["name"] for tool in results["mandatory"] if not tool["path"]]
    if missing:
        raise MissingDependencyError(
            "Missing mandatory tool(s): "
            + ", ".join(missing)
            + ". Install them to ensure full functionality."
        )
    logging.info("All mandatory tools verified successfully.")
