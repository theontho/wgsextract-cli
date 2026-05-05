import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from wgsextract_cli.core import runtime
from wgsextract_cli.core.dependencies import (
    check_all_dependencies,
    required_dependency_tools,
)
from wgsextract_cli.core.messages import CLI_HELP
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
        "--memory", help="WSL memory limit, such as 24GB. Defaults to 75%% of RAM."
    )
    wsl_tune_parser.add_argument(
        "--processors",
        type=int,
        help="Number of CPU processors available to WSL. Defaults to 2/3 of logical CPUs.",
    )
    wsl_tune_parser.add_argument(
        "--swap", help="WSL swap size, such as 16GB. Defaults to 25%% of RAM."
    )
    wsl_tune_parser.set_defaults(func=run_wsl_tune)

    for mode in ("cygwin", "msys2"):
        spec = runtime.bundled_runtime_spec(mode)
        bundled_parser = deps_subparsers.add_parser(
            mode,
            parents=[base_parser],
            help=f"Set up or check the bundled Windows {spec.display_name} runtime.",
        )
        bundled_subparsers = bundled_parser.add_subparsers(
            dest=f"{mode}_command", required=True
        )

        bundled_check_parser = bundled_subparsers.add_parser(
            "check", help=f"Check {spec.display_name} runtime availability."
        )
        bundled_check_parser.set_defaults(func=run_bundled_runtime_check, mode=mode)

        bundled_setup_parser = bundled_subparsers.add_parser(
            "setup", help=f"Download and extract {spec.display_name} into runtime/."
        )
        bundled_setup_parser.add_argument(
            "--url",
            help="Direct runtime ZIP URL. Overrides the release JSON lookup.",
        )
        bundled_setup_parser.add_argument(
            "--latest-json-url",
            default=DEFAULT_WINDOWS_RUNTIME_RELEASE_URL,
            help="Release JSON URL containing cygwin64/msys2 package URLs.",
        )
        bundled_setup_parser.add_argument(
            "--cache-dir",
            help="Directory used to cache downloaded runtime ZIP packages.",
        )
        bundled_setup_parser.add_argument(
            "--archive-dir",
            help="Directory containing pre-downloaded runtime ZIP packages.",
        )
        bundled_setup_parser.add_argument(
            "--source-dir",
            help=(
                "Directory containing an already-built runtime tree to copy. "
                "May be the runtime directory itself or a parent containing "
                f"{spec.dirname}/."
            ),
        )
        bundled_setup_parser.add_argument(
            "--refresh-download",
            action="store_true",
            help="Re-download the runtime ZIP even when it already exists in the cache.",
        )
        bundled_setup_parser.add_argument(
            "--force",
            action="store_true",
            help="Replace the existing extracted runtime directory if present.",
        )
        bundled_setup_parser.set_defaults(func=run_bundled_runtime_setup, mode=mode)

    pacman_parser = deps_subparsers.add_parser(
        "pacman",
        parents=[base_parser],
        help="Check native Windows bio tools installed by MSYS2 UCRT64 pacman.",
    )
    pacman_subparsers = pacman_parser.add_subparsers(
        dest="pacman_command", required=True
    )
    pacman_check_parser = pacman_subparsers.add_parser(
        "check", help="Check MSYS2 UCRT64 pacman-installed tool availability."
    )
    pacman_check_parser.set_defaults(func=run_pacman_check)


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


def run_bundled_runtime_setup(args: Any) -> None:
    mode = str(args.mode)
    spec = runtime.bundled_runtime_spec(mode)
    runtime_dir = runtime.bundled_runtime_dir(mode)
    bash_path = runtime.bundled_runtime_bash(mode)

    if runtime_dir.exists() and bash_path.exists() and not args.force:
        print(f"{spec.display_name} runtime already exists at {runtime_dir}")
        print("Use --force to replace it.")
        return

    if runtime_dir.exists() and args.force:
        shutil.rmtree(runtime_dir)

    runtime.runtime_root().mkdir(parents=True, exist_ok=True)
    if getattr(args, "source_dir", None):
        _copy_bundled_runtime_from_source(Path(args.source_dir), mode, runtime_dir)
    else:
        archive_path = _resolve_bundled_runtime_archive(args, mode)

        print(f"Extracting into {runtime.runtime_root()}")
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract_zip(archive, runtime.runtime_root())

    _clear_bundled_runtime_caches()
    _post_extract_bundled_runtime(mode)

    if not bash_path.exists():
        raise WGSExtractError(
            f"Downloaded archive did not create expected shell: {bash_path}"
        )

    _clear_bundled_runtime_caches()
    print(f"{spec.display_name} runtime ready at {runtime_dir}")
    _print_bundled_runtime_tool_status(mode, fail_on_missing=False)


def run_bundled_runtime_check(args: Any) -> None:
    mode = str(args.mode)
    spec = runtime.bundled_runtime_spec(mode)

    print(f"{spec.display_name} Runtime")
    print("-" * 60)
    print(f"Host platform:       {sys.platform}")
    print(f"Configured runtime:  {runtime.get_tool_runtime_mode()}")
    print(f"Runtime root:        {runtime.bundled_runtime_dir(mode)}")
    print(f"Shell:               {runtime.bundled_runtime_bash(mode)}")

    available = runtime.detect_bundled_runtime_available(mode, force=True)
    print(f"Runtime available:   {'yes' if available else 'no'}")
    if not available:
        raise WGSExtractError(
            f"{spec.display_name} is not available. Run "
            f"'wgsextract deps {mode} setup' to download the bundled runtime."
        )

    _print_bundled_runtime_tool_status(mode, fail_on_missing=True)


def run_pacman_check(args: Any) -> None:
    print("MSYS2 Pacman Runtime")
    print("-" * 60)
    print(f"Host platform:       {sys.platform}")
    print(f"Configured runtime:  {runtime.get_tool_runtime_mode()}")
    print("Pacman tool bin dirs:")
    for tool_bin in runtime.pacman_tool_bin_dirs():
        print(f"  {tool_bin}")

    pacman_path = _pacman_executable_path()
    print(f"Pacman executable:   {pacman_path or 'not found'}")

    print("\nMandatory tools")
    print("-" * 60)
    missing: list[str] = []
    for tool in required_dependency_tools(include_python=False):
        tool_path = runtime.pacman_tool_path(tool)
        print(f"{tool:<20} {'yes' if tool_path else 'no':<4} {tool_path or ''}")
        if not tool_path:
            missing.append(tool)

    if missing:
        packages = _pacman_packages_for_tools(missing)
        if packages:
            print("\nSuggested MSYS2 UCRT64 command:")
            print("pacman -S --needed " + " ".join(packages))
        notes = _pacman_notes_for_tools(missing)
        if notes:
            print("\nAdditional notes:")
            for note in notes:
                print(f"- {note}")
        raise WGSExtractError("Missing pacman-backed tool(s): " + ", ".join(missing))


def _pacman_packages_for_tools(tools: list[str]) -> list[str]:
    packages = []
    seen: set[str] = set()
    for tool in tools:
        package = PACMAN_TOOL_PACKAGES.get(tool)
        if package and package not in seen:
            seen.add(package)
            packages.append(package)
    return packages


def _pacman_notes_for_tools(tools: list[str]) -> list[str]:
    notes = []
    seen: set[str] = set()
    for tool in tools:
        note = PACMAN_TOOL_NOTES.get(tool)
        if note and note not in seen:
            seen.add(note)
            notes.append(note)
    return notes


def _pacman_executable_path() -> Path | None:
    for usr_bin in runtime.pacman_usr_bin_dirs():
        candidate = usr_bin / "pacman.exe"
        if candidate.exists():
            return candidate
    path = shutil.which("pacman") or shutil.which("pacman.exe")
    return Path(path) if path else None


def _copy_bundled_runtime_from_source(
    source_dir: Path, mode: str, runtime_dir: Path
) -> None:
    spec = runtime.bundled_runtime_spec(mode)
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


def _source_runtime_copy_ignore(mode: str) -> Any:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        if mode == "cygwin":
            return {name for name in names if name == "mnt"}
        return set()

    return ignore


def _clear_bundled_runtime_caches() -> None:
    runtime.detect_bundled_runtime_available.cache_clear()
    runtime.bundled_command_available.cache_clear()


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


def _bundled_command_available_with_retry(mode: str, tool: str) -> bool:
    for attempt in range(3):
        if attempt:
            time.sleep(0.2)
        _clear_bundled_runtime_caches()
        if runtime.bundled_command_available(mode, tool):
            return True
    return False


def _post_extract_bundled_runtime(mode: str) -> None:
    if mode != "cygwin" or runtime.bundled_runtime_bash(mode).exists():
        return

    root = runtime.runtime_root()
    setup_exe = root / "setup-x86_64.exe"
    runtime_dir = runtime.bundled_runtime_dir(mode)
    mirror_dir = _cygwin_local_mirror_dir(root)
    if not setup_exe.exists() or mirror_dir is None:
        return

    logs_dir = runtime.repo_root() / "tmp" / "runtime_setup"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / "cygwin_setup.stdout.log"
    stderr_log = logs_dir / "cygwin_setup.stderr.log"
    packages = ",".join(
        [
            "jq",
            "p7zip",
            "unzip",
            "zip",
            "libbz2-devel",
            "libzip-devel",
            "liblzma-devel",
            "libdeflate-devel",
            "zlib-devel",
            "libncurses-devel",
            "libcurl-devel",
            "libssl-devel",
        ]
    )
    command = [
        str(setup_exe),
        "--root",
        str(runtime_dir),
        "--site",
        mirror_dir.name,
        "--only-site",
        "--quiet-mode",
        "--no-shortcuts",
        "--no-admin",
        "--local-package-dir",
        str(root),
        "--local-install",
        "--categories",
        "base",
        "--packages",
        packages,
    ]

    print("Running Cygwin local package setup")
    print(f"  stdout: {stdout_log}")
    print(f"  stderr: {stderr_log}")
    with (
        stdout_log.open("w", encoding="utf-8") as stdout,
        stderr_log.open("w", encoding="utf-8") as stderr,
    ):
        completed = subprocess.run(
            command,
            cwd=str(root),
            stdout=stdout,
            stderr=stderr,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise WGSExtractError(
            f"Cygwin local package setup failed. See {stdout_log} and {stderr_log}."
        )


def _cygwin_local_mirror_dir(root: Path) -> Path | None:
    mirror_dir = root / "mirror"
    if mirror_dir.exists():
        return mirror_dir

    for candidate in root.iterdir():
        if not candidate.is_dir():
            continue
        if (
            candidate.name.lower().startswith("http")
            and "cygwin" in candidate.name.lower()
        ):
            candidate.rename(mirror_dir)
            return mirror_dir
    return None


def _bundled_runtime_archive_url(mode: str, latest_json_url: str) -> str:
    spec = runtime.bundled_runtime_spec(mode)
    request = urllib.request.Request(
        latest_json_url, headers={"User-Agent": DOWNLOAD_USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    try:
        archive_url = data[spec.archive_key]["URL"]
    except (KeyError, TypeError) as exc:
        raise WGSExtractError(
            f"Release JSON does not contain a URL for {spec.archive_key}: {latest_json_url}"
        ) from exc
    if not isinstance(archive_url, str) or not archive_url:
        raise WGSExtractError(
            f"Release JSON contains an empty URL for {spec.archive_key}: {latest_json_url}"
        )
    return archive_url


def _resolve_bundled_runtime_archive(args: Any, mode: str) -> Path:
    local_archive = _find_local_runtime_archive(
        mode,
        Path(args.archive_dir).expanduser().resolve() if args.archive_dir else None,
        str(args.url) if args.url else None,
    )
    if local_archive:
        _require_zipfile(local_archive)
        print(f"Using local runtime package: {local_archive}")
        return local_archive

    archive_url = args.url or _bundled_runtime_archive_url(
        mode, str(args.latest_json_url)
    )
    cache_dir = (
        Path(args.cache_dir).expanduser().resolve()
        if args.cache_dir
        else runtime.runtime_root() / "downloads"
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / _archive_filename(archive_url, mode)

    if archive_path.exists() and not args.refresh_download:
        if zipfile.is_zipfile(archive_path):
            print(f"Using cached runtime package: {archive_path}")
            return archive_path
        print(f"Ignoring invalid cached runtime package: {archive_path}")
        archive_path.unlink()

    print(
        f"Downloading {runtime.bundled_runtime_spec(mode).display_name} runtime package"
    )
    print(f"  from: {archive_url}")
    print(f"  to:   {archive_path}")
    _download_file(archive_url, archive_path)
    return archive_path


def _find_local_runtime_archive(
    mode: str, archive_dir: Path | None, archive_url: str | None = None
) -> Path | None:
    if archive_dir is None:
        return None
    if not archive_dir.is_dir():
        raise WGSExtractError(
            f"Runtime archive directory does not exist: {archive_dir}"
        )

    if archive_url:
        exact = archive_dir / _archive_filename(archive_url, mode)
        if exact.exists():
            return exact

    spec = runtime.bundled_runtime_spec(mode)
    candidates = sorted(
        {
            *archive_dir.glob(f"{spec.dirname}*.zip"),
            *archive_dir.glob(f"{spec.archive_key}*.zip"),
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _archive_filename(url: str, mode: str) -> str:
    parsed = urllib.parse.urlparse(url)
    filename = Path(urllib.parse.unquote(parsed.path)).name
    return filename if filename else f"{runtime.bundled_runtime_spec(mode).dirname}.zip"


def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)

        request = urllib.request.Request(
            url, headers={"User-Agent": DOWNLOAD_USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            with temp_path.open("wb") as output:
                shutil.copyfileobj(response, output)

        _require_zipfile(temp_path)
        temp_path.replace(destination)
    except Exception as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise WGSExtractError(f"Failed to download {url}: {exc}") from exc


def _require_zipfile(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise WGSExtractError(f"Runtime package is not a valid ZIP archive: {path}")
