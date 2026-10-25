import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import user_data_path

from wgsextract_cli.core.paths import windows_local_app_dir

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

MACOS_PERFORMANCE_LEVEL_NAMES = {"performance", "super"}


@dataclass(frozen=True)
class ThreadTuningProfile:
    threads: int
    label: str
    reason: str


def is_windows_host() -> bool:
    return sys.platform == "win32"


def _sysctl_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _macos_named_performance_core_count() -> int | None:
    output = _sysctl_output(["sysctl", "-a"])
    if output is None:
        return None

    perf_levels: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            key, separator, value = line.partition("=")
        if not separator:
            continue

        key_parts = key.strip().split(".")
        if len(key_parts) != 3 or key_parts[0] != "hw":
            continue
        level_key, field = key_parts[1], key_parts[2]
        if not level_key.startswith("perflevel") or field not in {
            "name",
            "physicalcpu",
        }:
            continue
        perf_levels.setdefault(level_key, {})[field] = value.strip().strip('"')

    total = 0
    for level_fields in perf_levels.values():
        if (
            level_fields.get("name", "").strip().lower()
            not in MACOS_PERFORMANCE_LEVEL_NAMES
        ):
            continue
        core_count = _positive_int(level_fields.get("physicalcpu", ""))
        if core_count:
            total += core_count
    return total or None


def macos_performance_core_count() -> int | None:
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        return None

    named_core_count = _macos_named_performance_core_count()
    if named_core_count:
        return named_core_count

    output = _sysctl_output(["sysctl", "-n", "hw.perflevel0.physicalcpu"])
    if output is None:
        return None
    return _positive_int(output.strip())


def default_thread_tuning_profile() -> ThreadTuningProfile:
    """Return the central default thread policy for user-facing commands."""
    apple_perf_cores = macos_performance_core_count()
    if apple_perf_cores:
        return ThreadTuningProfile(
            threads=apple_perf_cores,
            label=str(apple_perf_cores),
            reason="Apple Silicon performance-core count",
        )

    total_cpus = os.cpu_count() or 4
    return ThreadTuningProfile(
        threads=max(1, total_cpus),
        label=str(max(1, total_cpus)),
        reason="all available cores",
    )


PROJECT_NAME = "wgsextract-cli"


@lru_cache(maxsize=1)
def source_checkout_root() -> Path | None:
    """Return the wgsextract-cli source tree this package runs from, if any.

    Only the nearest enclosing pyproject.toml is considered, and only when it
    declares this project, so an installed copy that happens to live inside
    some other repository never adopts that repository's directories.
    """
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            with pyproject.open("rb") as f:
                name = tomllib.load(f).get("project", {}).get("name")
        except (OSError, tomllib.TOMLDecodeError):
            return None
        return parent if name == PROJECT_NAME else None
    return None


def app_data_root() -> Path:
    """Per-user data directory used when not running from a source checkout."""
    return windows_local_app_dir(PROJECT_NAME, "WGSExtract") or user_data_path(
        PROJECT_NAME, "WGSExtract"
    )


def repo_root() -> Path:
    """Base for default data folders (reference/, genomes/, jartools/): the
    source checkout when running from one, else the per-user data directory."""
    return source_checkout_root() or app_data_root()
