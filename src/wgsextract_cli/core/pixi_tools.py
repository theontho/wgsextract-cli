"""Running tools from the app's Pixi environments."""

import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from wgsextract_cli.core import runtime
from wgsextract_cli.core.paths import absolute_path, expand_path, java_local_path
from wgsextract_cli.core.tool_catalog import (
    JAVA_JAR,
    PIXI_TOOL_ENVS,
    PIXI_TOOL_PACKAGES,
    WINDOWS_PIXI_SCRIPT_LAUNCHERS,
    _tool_executable_names,
)


def is_pixi_tool_command(path: str) -> bool:
    """Return whether a resolved command is explicitly launched through Pixi."""
    try:
        command = shlex.split(expand_path(path), posix=not runtime.is_windows_host())
    except ValueError:
        return False
    if not command:
        return False
    executable = Path(command[0]).name.lower()
    # Windows package managers may install pixi as a .cmd or .bat shim.
    pixi_names = {"pixi", "pixi.exe", "pixi.cmd", "pixi.bat"}
    return executable in pixi_names and "run" in command[1:]


def is_pixi_tool_path(path: str) -> bool:
    """Return whether a resolved executable lives inside the active Pixi env."""
    expanded_path = str(absolute_path(expand_path(path)))
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
        if candidate and Path(candidate).is_file():
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
        expanded = expand_path(path)
        if Path(expanded).exists():
            return expanded
    return None


def _pixi_manifest_args() -> list[str]:
    """Point pixi at this app's manifest; the CLI may run from any directory."""
    root = runtime.source_checkout_root()
    if root and (root / "pixi.toml").is_file():
        return ["--manifest-path", str(root / "pixi.toml")]
    return []


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
            errors="replace",
            timeout=60,
        )
        packages = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    package = PIXI_TOOL_PACKAGES.get(tool, tool)
    return any(entry.get("name") == package for entry in packages)


def _join_command(command: list[str]) -> str:
    if runtime.is_windows_host():
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _pixi_run_command(pixi_cmd: str, env: str, args: list[str]) -> str:
    return _join_command(
        [pixi_cmd, "run", "--no-install", *_pixi_manifest_args(), "-e", env, *args]
    )


def pixi_install_command(tool: str) -> str | None:
    """An explicit, checkout-independent repair command for a packaged tool."""
    env = PIXI_TOOL_ENVS.get(tool)
    if env is None:
        return None
    return _join_command(
        ["pixi", "install", "--locked", *_pixi_manifest_args(), "-e", env]
    )


def _probe_environment(prefix: str, tool_names: list[str], windows: bool) -> str | None:
    """How to run a tool from the Pixi environment at prefix, as "kind:value"
    (direct-name, haplogrep or script), or None.

    Runs in this process or, for another environment, inside it: its source is
    sent to that environment's Python, so it imports what it needs itself.
    """
    import json
    import shutil
    import zipfile
    from pathlib import Path

    root = Path(prefix)
    if windows and "gatk" in tool_names:
        jars = sorted(root.glob("share/gatk4-*/gatk-package-4.6.2.0-local.jar"))
        sidecar = root / "Library/share/gkl/gkl-native-wgse.jar"
        java = root / "Library" / "bin" / "java.exe"
        if not jars or not sidecar.is_file() or not java.is_file():
            return None
        try:
            with zipfile.ZipFile(sidecar) as native:
                required = {
                    f"com/intel/gkl/native/{name}.dll"
                    for name in (
                        "gkl_utils",
                        "gkl_pairhmm",
                        "gkl_pairhmm_omp",
                        "gkl_smithwaterman",
                    )
                }
                if not required.issubset(native.namelist()):
                    return None
        except (OSError, zipfile.BadZipFile):
            return None
        # The upstream launcher uses -jar, which ignores an added classpath.
        # Preserve its packaged JVM options and default heap ergonomics.
        return "gatk:" + json.dumps(
            [
                str(java),
                "-Dstdout.encoding=UTF-8",
                "-Dstderr.encoding=UTF-8",
                "-Dsamjdk.use_async_io_read_samtools=false",
                "-Dsamjdk.use_async_io_write_samtools=true",
                "-Dsamjdk.use_async_io_write_tribble=false",
                "-Dsamjdk.compression_level=2",
                "-cp",
                f"{sidecar};{jars[-1]}",
                "org.broadinstitute.hellbender.Main",
            ]
        )

    for tool in tool_names:
        if shutil.which(tool):
            return "direct-name:" + tool

    if "haplogrep" in tool_names:
        jar = root / "bin" / "haplogrep.jar"
        if jar.exists():
            return "haplogrep:" + str(jar.resolve())

    for tool in tool_names:
        # noarch packages keep the real script beside its data, in share/<pkg>/
        # (GATK's jar) or opt/<pkg>/ (FastQC's), and link it into bin/; Windows
        # gets no usable link, so look there first.
        shared = sorted(
            p
            for d in ("share", "opt")
            for p in root.glob(d + "/*/" + tool)
            if p.is_file()
        )
        if windows and shared:
            return "script:" + str(shared[-1])
        script = root / "bin" / tool
        if script.exists() or script.is_symlink():
            return "script:" + str(script.resolve())
    return None


def _probe_environment_with_pixi(
    pixi_cmd: str, env: str, tool_names: list[str]
) -> subprocess.CompletedProcess[str] | None:
    """Run _probe_environment inside another Pixi environment."""
    script = (
        inspect.getsource(_probe_environment)
        + "\nimport os, sys\n"
        + "found = _probe_environment(sys.prefix, sys.argv[1:], os.name == 'nt')\n"
        + "print(found or '')\n"
        + "raise SystemExit(0 if found else 1)\n"
    )
    try:
        return subprocess.run(
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
                script,
                *tool_names,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            # A cold first start on Windows takes far longer than later ones.
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _host_pixi_tool_command(tool: str) -> str | None:
    if tool not in PIXI_TOOL_ENVS:
        return None

    env = PIXI_TOOL_ENVS[tool]
    pixi_cmd = _resolve_pixi_command()
    if not pixi_cmd:
        return None

    executable_names = _tool_executable_names(tool)
    # Look in the environment the CLI runs in first (`dev` has all of
    # `default`), and run from it directly: `pixi run` takes seconds on Windows.
    # Interpreter aliases need their dedicated environment's Python packages.
    current = os.environ.get("PIXI_ENVIRONMENT_NAME")
    found = (
        _probe_environment(
            sys.prefix, list(executable_names), runtime.is_windows_host()
        )
        if current and (current == env or executable_names != ("python",))
        else None
    )
    in_env = bool(found) or current == env
    if not in_env:
        result = _probe_environment_with_pixi(pixi_cmd, env, list(executable_names))
        if result is None:
            return None
        lines = result.stdout.strip().splitlines()
        found = lines[-1] if result.returncode == 0 and lines else None

    def command(args: list[str]) -> str:
        if not in_env:
            return _pixi_run_command(pixi_cmd, env, args)
        program = sys.executable if args[0] == "python" else shutil.which(args[0])
        return _join_command([program or args[0], *args[1:]])

    if not found:
        return None

    kind, separator, value = found.partition(":")
    if not separator or not value:
        return None

    if kind == "direct-name":
        return command([value])
    if kind == "gatk":
        return command(json.loads(value))
    if kind == "haplogrep":
        return command([*JAVA_JAR, java_local_path(value)])
    if kind == "script" and tool in WINDOWS_PIXI_SCRIPT_LAUNCHERS:
        return command([*WINDOWS_PIXI_SCRIPT_LAUNCHERS[tool], value])
    if kind == "script" and not runtime.is_windows_host():
        return command([value])
    return None


def pixi_env_tool(tool: str) -> str | None:
    """How to run a tool from its Pixi environment only (not from PATH)."""
    return _host_pixi_tool_command(tool)


def windows_gatk_java_options(command: list[str]) -> list[str]:
    """Move launcher-only options before Main for direct Windows Java launches."""
    main = "org.broadinstitute.hellbender.Main"
    if (
        not runtime.is_windows_host()
        or main not in command
        or "--java-options" not in command
    ):
        return command
    index = command.index(main)
    arguments = command[index + 1 :]
    options: list[str] = []
    remaining: list[str] = []
    i = 0
    while i < len(arguments):
        if arguments[i] == "--java-options":
            if i + 1 == len(arguments):
                raise ValueError("GATK --java-options requires an option string.")
            # Match the upstream gatk Python launcher's whitespace splitting.
            options.extend(arguments[i + 1].split())
            i += 2
        else:
            remaining.append(arguments[i])
            i += 1
    return [*command[:index], *options, main, *remaining]


def pixi_env_prefix(env: str) -> Path | None:
    """An installed Pixi environment's prefix (for data its packages ship)."""
    pixi_cmd = _resolve_pixi_command()
    if not pixi_cmd:
        return None
    try:
        result = subprocess.run(
            [pixi_cmd, "info", *_pixi_manifest_args(), "--json"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
        envs = json.loads(result.stdout)["environments_info"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None
    for info in envs:
        if info.get("name") == env and info.get("prefix"):
            prefix = Path(info["prefix"])
            return prefix if prefix.is_dir() else None
    return None
