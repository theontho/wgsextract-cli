import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wgsextract_cli.core import (
    runtime,
    runtime_paths,
    runtime_wrappers,
)
from wgsextract_cli.core.dependencies import required_dependency_tools
from wgsextract_cli.core.dependency_checks import check_all_dependencies
from wgsextract_cli.core.utils import WGSExtractError

DEFAULT_WINDOWS_RUNTIME_RELEASE_URL = "https://get.wgse.io/latest-release-Beta.json"


DOWNLOAD_USER_AGENT = "wgsextract-cli-runtime-setup/1.0"


PACMAN_TOOL_PACKAGES = {
    "samtools": "mingw-w64-ucrt-x86_64-samtools",
    "bcftools": "mingw-w64-ucrt-x86_64-bcftools",
    "tabix": "mingw-w64-ucrt-x86_64-htslib",
    "bgzip": "mingw-w64-ucrt-x86_64-htslib",
    "htsfile": "mingw-w64-ucrt-x86_64-htslib",
    "gzip": "gzip",
    "tar": "tar",
}


PACMAN_TOOL_NOTES = {
    "bwa": "MSYS2 UCRT64 does not currently publish a BWA package; "
    "build or place bwa.exe in the UCRT64 bin directory.",
}


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
            raise WGSExtractError(f"Tool not found: {args.tool}")

    logging.info("Verifying bioinformatics tool installations...")
    results = check_all_dependencies()

    print("\nMandatory Tools:")
    print("-" * 60)
    all_mandatory_present = True
    for tool in results["mandatory"]:
        status = _status_text(tool["path"], alt_env=bool(tool.get("alt_env")))
        source = _tool_source(tool)
        version = f" ({tool['version']})" if tool["version"] else ""
        print(f"{status} {tool['name']:<20} {source}{version}")
        if not tool["path"]:
            all_mandatory_present = False

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
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.replace("\x00", "").strip()


def _check_dependencies_with_runtime(mode: str) -> dict[str, list[dict[str, Any]]]:
    previous = os.environ.get(runtime.RUNTIME_ENV_VAR)
    os.environ[runtime.RUNTIME_ENV_VAR] = mode
    try:
        return check_all_dependencies()
    finally:
        if previous is None:
            os.environ.pop(runtime.RUNTIME_ENV_VAR, None)
        else:
            os.environ[runtime.RUNTIME_ENV_VAR] = previous


def run_wsl_check(args: argparse.Namespace) -> None:
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
        print(f"Current repo in WSL: {runtime_paths.windows_to_wsl_path(os.getcwd())}")

    config_path = runtime_wrappers.get_wslconfig_path()
    settings = runtime_wrappers.read_wslconfig_settings(config_path)
    print(f".wslconfig:          {config_path}")
    for key in ["memory", "processors", "swap"]:
        print(f"  {key:<10}       {settings.get(key, 'unset')}")

    print("\nMandatory tools")
    print("-" * 60)
    results = _check_dependencies_with_runtime("wsl")
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


def run_wsl_tune(args: argparse.Namespace) -> None:
    recommendation = runtime_wrappers.recommend_wslconfig_settings()
    memory = args.memory or recommendation.memory
    processors = (
        args.processors if args.processors is not None else recommendation.processors
    )
    swap = args.swap or recommendation.swap

    config_path = runtime_wrappers.write_wslconfig_settings(
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


def _copy_sibling_runtime_tools(source_runtime_dir: Path, runtime_dir: Path) -> None:
    for dirname in ("FastQC", "jre8"):
        source_tool_dir = source_runtime_dir.parent / dirname
        destination_tool_dir = runtime_dir / dirname
        if not source_tool_dir.is_dir() or destination_tool_dir.exists():
            continue

        print(f"Copying {dirname} runtime tools from {source_tool_dir}")
        shutil.copytree(source_tool_dir, destination_tool_dir)


def _patch_fastqc_launcher(runtime_dir: Path) -> None:
    launcher = runtime_dir / "FastQC" / "fastqc"
    if not launcher.exists():
        return

    text = launcher.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    patched = re.sub(
        r'"-jar\s+\$RealBin/FastQC\.jar"',
        '"-jar", "$RealBin/FastQC.jar"',
        normalized,
    )

    java_tail = re.compile(
        r"if\s*\(\s*\$java_bin\s+ne\s+['\"]java['\"]\s*\)\s*\{\s*"
        r"system\s+\$java_bin,\s*@java_args,\s*"
        r'"-jar",\s*"\$RealBin/FastQC\.jar",\s*@files;\s*'
        r"\}\s*else\s*\{\s*"
        r"exec\s+\$java_bin,\s*@java_args,\s*"
        r'"-jar",\s*"\$RealBin/FastQC\.jar",\s*@files;\s*'
        r"\}",
        re.MULTILINE,
    )
    replacement = (
        'my $fastqc_jar = "$RealBin/FastQC.jar";\n'
        "if ($^O eq 'cygwin') {\n"
        '\tmy $converted_jar = `cygpath -w "$fastqc_jar" 2>/dev/null`;\n'
        "\tchomp $converted_jar;\n"
        "\t$fastqc_jar = $converted_jar if $converted_jar;\n"
        "}\n"
        "\n"
        "if ($java_bin ne 'java') {\n"
        '\tsystem $java_bin, @java_args, "-jar", $fastqc_jar, @files;\n'
        "}\n"
        "else {\n"
        '\texec $java_bin, @java_args, "-jar", $fastqc_jar, @files;\n'
        "}\n"
    )
    if "$fastqc_jar" not in patched:
        patched = java_tail.sub(lambda _match: replacement, patched, count=1)
    if patched == text:
        return

    launcher.write_text(patched, encoding="utf-8")


def _source_runtime_copy_ignore(mode: str) -> Callable[[str, list[str]], set[str]]:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        if mode == "cygwin":
            return {name for name in names if name == "mnt"}
        return set()

    return ignore


def _copy_bundled_runtime_from_source(
    source_dir: Path, mode: str, runtime_dir: Path
) -> None:
    spec = runtime_paths.bundled_runtime_spec(mode)
    source = source_dir.expanduser().resolve()
    candidate = source / spec.dirname if (source / spec.dirname).exists() else source
    expected_shell = candidate / Path(spec.bash_relpath)
    if not expected_shell.exists():
        raise WGSExtractError(
            f"Runtime source does not contain expected shell for {spec.display_name}: "
            f"{expected_shell}"
        )

    print(f"Copying {spec.display_name} runtime")
    print(f"  from: {candidate}")
    print(f"  to:   {runtime_dir}")
    shutil.copytree(
        candidate,
        runtime_dir,
        ignore=_source_runtime_copy_ignore(mode),
    )
    _copy_sibling_runtime_tools(candidate, runtime_dir)
    _patch_fastqc_launcher(runtime_dir)


def _clear_bundled_runtime_caches() -> None:
    runtime_paths.detect_bundled_runtime_available.cache_clear()
    runtime_paths.bundled_command_available.cache_clear()


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        member_name = member.filename.replace("\\", "/")
        target = (root / member_name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise WGSExtractError(
                f"Runtime archive contains unsafe path: {member.filename}"
            ) from exc

    archive.extractall(root)


def _bundled_command_available_with_retry(mode: str, tool: str) -> bool:
    for attempt in range(3):
        if attempt:
            time.sleep(0.2)
        _clear_bundled_runtime_caches()
        if runtime_paths.bundled_command_available(mode, tool):
            return True
    return False


def _print_bundled_runtime_tool_status(
    mode: str, *, fail_on_missing: bool = True
) -> None:
    print("\nMandatory tools")
    print("-" * 60)
    missing: list[str] = []
    for tool in required_dependency_tools(include_python=False):
        present = _bundled_command_available_with_retry(mode, tool)
        print(f"{tool:<20} {'yes' if present else 'no'}")
        if not present:
            missing.append(tool)

    if missing and fail_on_missing:
        raise WGSExtractError("Missing bundled tool(s): " + ", ".join(missing))
    if missing:
        print("\nMissing bundled tool(s): " + ", ".join(missing))
        print(
            f"Run 'wgsextract deps {mode} check' after adding the bio tool collection."
        )
