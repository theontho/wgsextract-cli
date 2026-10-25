import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from wgsextract_cli.core import (
    runtime,
    runtime_paths,
)
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.download_progress import (
    copy_response_to_file,
    require_http_url,
)
from wgsextract_cli.core.tool_catalog import required_dependency_tools
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

    launcher.write_text(patched, encoding="utf-8", newline="\n")


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
    clear_tool_caches()


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


def _post_extract_bundled_runtime(mode: str) -> None:
    if mode != "cygwin" or runtime_paths.bundled_runtime_bash(mode).exists():
        return

    root = runtime_paths.runtime_root()
    setup_exe = root / "setup-x86_64.exe"
    runtime_dir = runtime_paths.bundled_runtime_dir(mode)
    mirror_dir = _cygwin_local_mirror_dir(root)
    if not setup_exe.exists() or mirror_dir is None:
        return

    logs_dir = root / "logs"
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
        stdout_log.open("w", encoding="utf-8", newline="\n") as stdout,
        stderr_log.open("w", encoding="utf-8", newline="\n") as stderr,
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


def _bundled_runtime_archive_url(mode: str, latest_json_url: str) -> str:
    require_http_url(latest_json_url, "runtime release JSON URL")
    spec = runtime_paths.bundled_runtime_spec(mode)
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


def _archive_filename(url: str, mode: str) -> str:
    parsed = urllib.parse.urlparse(url)
    filename = Path(urllib.parse.unquote(parsed.path)).name
    return (
        filename
        if filename
        else f"{runtime_paths.bundled_runtime_spec(mode).dirname}.zip"
    )


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

    spec = runtime_paths.bundled_runtime_spec(mode)
    candidates = sorted(
        {
            *archive_dir.glob(f"{spec.dirname}*.zip"),
            *archive_dir.glob(f"{spec.archive_key}*.zip"),
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _require_zipfile(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise WGSExtractError(f"Runtime package is not a valid ZIP archive: {path}")


def _download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        require_http_url(url, "runtime archive URL")
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
                copy_response_to_file(
                    response,
                    output,
                    progress_label=destination.name,
                )

        _require_zipfile(temp_path)
        temp_path.replace(destination)
    except (
        OSError,
        urllib.error.URLError,
        zipfile.BadZipFile,
        ValueError,
        WGSExtractError,
    ) as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise WGSExtractError(f"Failed to download {url}: {exc}") from exc


def _resolve_bundled_runtime_archive(args: argparse.Namespace, mode: str) -> Path:
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
        else runtime_paths.runtime_root() / "downloads"
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
        f"Downloading {runtime_paths.bundled_runtime_spec(mode).display_name} runtime package"
    )
    print(f"  from: {archive_url}")
    print(f"  to:   {archive_path}")
    _download_file(archive_url, archive_path)
    return archive_path


def _install_bundled_runtime(
    args: argparse.Namespace, mode: str, runtime_dir: Path
) -> None:
    if getattr(args, "source_dir", None):
        _copy_bundled_runtime_from_source(Path(args.source_dir), mode, runtime_dir)
    else:
        archive_path = _resolve_bundled_runtime_archive(args, mode)
        print(f"Extracting into {runtime_paths.runtime_root()}")
        with zipfile.ZipFile(archive_path) as archive:
            _safe_extract_zip(archive, runtime_paths.runtime_root())

    _clear_bundled_runtime_caches()
    _post_extract_bundled_runtime(mode)


def run_bundled_runtime_setup(args: argparse.Namespace) -> None:
    mode = str(args.mode)
    spec = runtime_paths.bundled_runtime_spec(mode)
    runtime_dir = runtime_paths.bundled_runtime_dir(mode)
    bash_path = runtime_paths.bundled_runtime_bash(mode)

    if runtime_dir.exists() and bash_path.exists() and not args.force:
        print(f"{spec.display_name} runtime already exists at {runtime_dir}")
        print("Use --force to replace it.")
        return

    runtime_paths.runtime_root().mkdir(parents=True, exist_ok=True)
    # Keep any existing runtime aside until the replacement is complete, so a
    # failed download or extraction leaves the user with a working runtime.
    backup_dir: Path | None = None
    if runtime_dir.exists():
        backup_dir = runtime_dir.with_name(f".{runtime_dir.name}.previous")
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        runtime_dir.rename(backup_dir)

    try:
        _install_bundled_runtime(args, mode, runtime_dir)
        if not bash_path.exists():
            raise WGSExtractError(
                f"Runtime setup did not create expected shell: {bash_path}"
            )
    except BaseException:
        if backup_dir is not None:
            print(f"Setup failed; restoring previous runtime at {runtime_dir}")
            if runtime_dir.exists():
                shutil.rmtree(runtime_dir)
            backup_dir.rename(runtime_dir)
        _clear_bundled_runtime_caches()
        raise
    if backup_dir is not None:
        shutil.rmtree(backup_dir)

    _clear_bundled_runtime_caches()
    print(f"{spec.display_name} runtime ready at {runtime_dir}")
    _print_bundled_runtime_tool_status(mode, fail_on_missing=False)


def run_bundled_runtime_check(args: argparse.Namespace) -> None:
    mode = str(args.mode)
    spec = runtime_paths.bundled_runtime_spec(mode)

    print(f"{spec.display_name} Runtime")
    print("-" * 60)
    print(f"Host platform:       {sys.platform}")
    print(f"Configured runtime:  {runtime.get_tool_runtime_mode()}")
    print(f"Runtime root:        {runtime_paths.bundled_runtime_dir(mode)}")
    print(f"Shell:               {runtime_paths.bundled_runtime_bash(mode)}")

    available = runtime_paths.detect_bundled_runtime_available(mode, force=True)
    print(f"Runtime available:   {'yes' if available else 'no'}")
    if not available:
        raise WGSExtractError(
            f"{spec.display_name} is not available. Run "
            f"'wgsextract deps {mode} setup' to download the bundled runtime."
        )

    _print_bundled_runtime_tool_status(mode, fail_on_missing=True)


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
    for usr_bin in runtime_paths.pacman_usr_bin_dirs():
        candidate = usr_bin / "pacman.exe"
        if candidate.exists():
            return candidate
    path = shutil.which("pacman") or shutil.which("pacman.exe")
    return Path(path) if path else None


def run_pacman_check(args: argparse.Namespace) -> None:
    print("MSYS2 Pacman Runtime")
    print("-" * 60)
    print(f"Host platform:       {sys.platform}")
    print(f"Configured runtime:  {runtime.get_tool_runtime_mode()}")
    print("Pacman tool bin dirs:")
    for tool_bin in runtime_paths.pacman_tool_bin_dirs():
        print(f"  {tool_bin}")

    pacman_path = _pacman_executable_path()
    print(f"Pacman executable:   {pacman_path or 'not found'}")

    print("\nMandatory tools")
    print("-" * 60)
    missing: list[str] = []
    for tool in required_dependency_tools(include_python=False):
        tool_path = runtime_paths.pacman_tool_path(tool)
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
