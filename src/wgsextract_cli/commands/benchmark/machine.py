import platform
import sys
from pathlib import Path
from typing import Any

import psutil

from .environment import (
    _command_output,
    _cpu_frequency,
    _cpu_model,
    _filesystem_mount,
    _first_matching_line,
    _linux_drive_model,
    _linux_sys_block_name,
    _ram_speed,
    _windows_cim_values,
)

MACHINE_STAT_FIELDS = (
    ("OS", "os"),
    ("Architecture", "architecture"),
    ("CPU", "cpu_model"),
    ("Cores", "cores"),
    ("CPU frequency", "cpu_frequency"),
    ("RAM", "ram"),
    ("RAM speed", "ram_speed"),
    ("Benchmark filesystem", "benchmark_filesystem"),
    ("Disk", "disk"),
    ("Drive", "drive"),
    ("Drive speed", "drive_speed"),
)


def _macos_storage_profile_for_path(path: Path) -> str | None:
    mount = _filesystem_mount(path)
    output = _command_output(["system_profiler", "SPStorageDataType"])
    if not output:
        return None
    blocks = output.split("\n\n")
    for block in blocks:
        if f"Mount Point: {mount}" in block:
            return block
    return None


def _drive_model(path: Path) -> str | None:
    system = platform.system()
    if system == "Darwin":
        output = _command_output(["diskutil", "info", _filesystem_mount(path)])
        model = _first_matching_line(output, "Device / Media Name:")
        if model:
            return model
        model = _first_matching_line(output, "Media Name:")
        if model:
            return model
        return _first_matching_line(
            _macos_storage_profile_for_path(path), "Device Name:"
        )
    if system == "Windows":
        return _windows_cim_values("Win32_DiskDrive", "$_.Model")
    if system == "Linux":
        return _linux_drive_model(path)
    return None


def _linux_drive_rotational(path: Path) -> bool | None:
    block_name = _linux_sys_block_name(path)
    if not block_name:
        return None
    rotational_path = Path("/sys/block") / block_name / "queue" / "rotational"
    if not rotational_path.exists():
        return None
    try:
        return rotational_path.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return None


def _drive_speed(path: Path) -> str | None:
    system = platform.system()
    if system == "Darwin":
        output = _command_output(["diskutil", "info", _filesystem_mount(path)])
        protocol = _first_matching_line(output, "Protocol:")
        if protocol:
            return protocol
        profile = _macos_storage_profile_for_path(path)
        medium = _first_matching_line(profile, "Medium Type:")
        protocol = _first_matching_line(profile, "Protocol:")
        if medium and protocol:
            return f"{medium}, {protocol}"
        return medium or protocol
    if system == "Windows":
        return _windows_cim_values(
            "Win32_DiskDrive", '"$($_.MediaType) ($($_.InterfaceType))"'
        )
    if system == "Linux":
        rotational = _linux_drive_rotational(path)
        if rotational is None:
            return None
        return "HDD/rotational" if rotational else "SSD/non-rotational"
    return None


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "bytes":
                return f"{size_bytes:,} bytes"
            return f"{size:.1f} {unit} ({size_bytes:,} bytes)"
        size /= 1024
    return f"{size_bytes:,} bytes"


def _machine_stats(run_dir: Path) -> dict[str, str | int | None]:
    virtual_memory = psutil.virtual_memory()
    disk_usage = psutil.disk_usage(str(run_dir))
    return {
        "os": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "architecture": platform.machine() or None,
        "cpu_model": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "cpu_frequency": _cpu_frequency(),
        "ram_total": _format_bytes(virtual_memory.total),
        "ram_available": _format_bytes(virtual_memory.available),
        "ram_speed": _ram_speed(),
        "benchmark_filesystem": str(run_dir.anchor or run_dir),
        "disk_total": _format_bytes(disk_usage.total),
        "disk_free": _format_bytes(disk_usage.free),
        "drive_model": _drive_model(run_dir),
        "drive_speed": _drive_speed(run_dir),
    }


def _machine_stat_display_value(
    stats: dict[str, str | int | None], key: str
) -> str | None:
    if key == "cores":
        physical = stats["physical_cores"] or "unknown"
        logical = stats["logical_cores"] or "unknown"
        return f"{physical} physical / {logical} logical"
    if key == "ram":
        return f"{stats['ram_total']} total / {stats['ram_available']} available"
    if key == "disk":
        return f"{stats['disk_total']} total / {stats['disk_free']} free"
    if key == "drive":
        drive_model = stats["drive_model"]
        return str(drive_model) if drive_model else None
    value = stats.get(key)
    return str(value) if value else None


def _print_machine_stats(stats: dict[str, str | int | None]) -> None:
    print("Machine stats:", flush=True)
    for label, key in MACHINE_STAT_FIELDS:
        value = _machine_stat_display_value(stats, key)
        if value:
            print(f"  {label}: {value}", flush=True)
    print("", flush=True)


def _record_base_file_size(metadata: dict[str, Any], base_file: Path) -> None:
    if not base_file.exists():
        metadata["base_file_size"] = "not available"
        metadata["base_file_size_bytes"] = None
        return

    size_bytes = base_file.stat().st_size
    metadata["base_file_size"] = _format_bytes(size_bytes)
    metadata["base_file_size_bytes"] = size_bytes
