import argparse
import os
import platform
import subprocess
import zipfile
from pathlib import Path

import psutil

from wgsextract_cli.core.dependencies import (
    get_tool_path,
    get_tool_runtime,
)
from wgsextract_cli.core.dependency_checks import get_tool_version
from wgsextract_cli.core.utils import WGSExtractError

from ._benchmark_datasets import (
    _download_file,
    _download_filename,
    _is_relative_to,
    _load_real_benchmark_dataset,
    _normalized_dataset_sha256,
    _prepare_direct_real_benchmark_dataset,
    _real_dataset_cache_dir,
    _sha256,
)
from ._benchmark_execution import (
    _tool_active_for_benchmark,
)
from ._benchmark_models import (
    BENCHMARK_EXTERNAL_TOOLS,
    DEFAULT_REAL_DATASET_URL,
    PROGRESS_STEP_WIDTH,
    REAL_BENCHMARK_DATASETS,
    BenchmarkDataset,
    BenchmarkResult,
    BenchmarkThreadPlan,
    _read_fai,
)


def _real_dataset_zip_path(args: argparse.Namespace, outdir: Path) -> Path:
    local_zip = getattr(args, "dataset_zip", None)
    if local_zip:
        path = Path(str(local_zip)).expanduser().resolve()
        if not path.is_file():
            raise WGSExtractError(f"Benchmark dataset zip not found: {path}")
        return path

    url = getattr(args, "dataset_url", None) or DEFAULT_REAL_DATASET_URL
    expected_sha256 = _normalized_dataset_sha256(args)
    cache_dir = _real_dataset_cache_dir(args, outdir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / _download_filename(url)
    if zip_path.exists() and not expected_sha256:
        return zip_path
    if zip_path.exists() and _sha256(zip_path) == expected_sha256:
        return zip_path

    _download_file(url, zip_path)
    return zip_path


def _verify_sha256(path: Path, expected: str) -> None:
    normalized = expected.lower().strip()
    if not normalized:
        return
    actual = _sha256(path)
    if actual != normalized:
        raise WGSExtractError(
            f"Checksum mismatch for {path}: expected {normalized}, got {actual}"
        )


def _extract_zip_safely(zip_path: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    root = extract_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                target = (extract_dir / member.filename).resolve()
                if not _is_relative_to(target, root):
                    raise WGSExtractError(
                        f"Unsafe benchmark dataset zip entry: {member.filename}"
                    )
            archive.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise WGSExtractError(f"Invalid benchmark dataset zip: {zip_path}") from exc


def _prepare_real_benchmark_dataset(
    args: argparse.Namespace, dataset_dir: Path, outdir: Path
) -> BenchmarkDataset:
    selected_dataset = getattr(args, "dataset", "real")
    spec = REAL_BENCHMARK_DATASETS.get(selected_dataset)
    if spec is None:
        raise WGSExtractError(f"Unknown real benchmark dataset: {selected_dataset}")
    if spec.kind == "direct":
        return _prepare_direct_real_benchmark_dataset(args, dataset_dir, outdir, spec)

    zip_path = _real_dataset_zip_path(args, outdir)
    expected_sha256 = _normalized_dataset_sha256(args)
    if expected_sha256:
        _verify_sha256(zip_path, expected_sha256)

    extract_dir = dataset_dir / "real"
    _extract_zip_safely(zip_path, extract_dir)
    return _load_real_benchmark_dataset(extract_dir)


def _matching_ref_contig(ref_path: Path, chrom: str) -> str | None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        return None
    contigs = [contig for contig, _length in _read_fai(fai_path)]
    if chrom in contigs:
        return chrom
    if chrom.startswith("chr"):
        without_chr = chrom[3:]
        if without_chr in contigs:
            return without_chr
    else:
        with_chr = f"chr{chrom}"
        if with_chr in contigs:
            return with_chr
    aliases = {
        "M": ("MT", "chrM", "chrMT"),
        "MT": ("M", "chrM", "chrMT"),
        "chrM": ("MT", "M", "chrMT"),
        "chrMT": ("MT", "M", "chrM"),
    }
    for alias in aliases.get(chrom, ()):
        if alias in contigs:
            return alias
    return None


def _normalize_region_for_ref(region: str | None, ref_path: Path) -> str | None:
    if not region:
        return None
    chrom, sep, range_part = region.partition(":")
    normalized_chrom = _matching_ref_contig(ref_path, chrom) or chrom
    return f"{normalized_chrom}:{range_part}" if sep else normalized_chrom


def _print_progress_header() -> None:
    print("WGSExtract CLI Benchmark Progress", flush=True)
    print(f"{'Step':<{PROGRESS_STEP_WIDTH}} {'Status':<6} {'Seconds':>10}", flush=True)
    print("-" * (PROGRESS_STEP_WIDTH + 19), flush=True)


def _print_thread_policy(thread_plan: BenchmarkThreadPlan) -> None:
    print("Thread policy:", flush=True)
    print(f"  Threads: {thread_plan.label}", flush=True)
    print(f"  Policy: {thread_plan.reason}", flush=True)
    print("", flush=True)


def _format_progress_result(result: BenchmarkResult) -> str:
    return f"{result.name:<{PROGRESS_STEP_WIDTH}} {result.status:<6} {result.seconds:>10.2f}"


def _print_failure_log_excerpt(result: BenchmarkResult) -> None:
    if not result.stderr_log:
        return
    stderr_path = Path(result.stderr_log)
    if not stderr_path.exists():
        return
    lines = stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return
    print(f"Failure stderr excerpt ({stderr_path}):", flush=True)
    for line in lines[-40:]:
        print(f"  {line}", flush=True)


def _tool_status(required: bool, active: bool, path: str | None) -> str:
    if active:
        return "active"
    if path:
        return "available, not used on this platform"
    if required:
        return "missing required tool"
    return "missing optional tool"


def _benchmark_external_tools() -> list[dict[str, str | bool | None]]:
    tools: list[dict[str, str | bool | None]] = []
    for spec in BENCHMARK_EXTERNAL_TOOLS:
        path = get_tool_path(spec.name)
        version = get_tool_version(spec.name) if path else None
        active = _tool_active_for_benchmark(spec.name, path)
        tools.append(
            {
                "name": spec.name,
                "required": spec.required,
                "active": active,
                "status": _tool_status(spec.required, active, path),
                "purpose": spec.purpose,
                "path": path,
                "runtime": get_tool_runtime(path),
                "version": version,
            }
        )
    return tools


def _format_tool_line(tool: dict[str, str | bool | None]) -> str:
    requirement = "required" if tool["required"] else "optional"
    path = tool["path"] or "missing"
    runtime = tool["runtime"] or "missing"
    version = tool["version"] or "version unavailable"
    return (
        f"{tool['name']} [{requirement}, {tool['status']}] - {tool['purpose']} | "
        f"{runtime}: {path} | {version}"
    )


def _print_external_tools(tools: list[dict[str, str | bool | None]]) -> None:
    print("External tools used or checked by this benchmark:", flush=True)
    for tool in tools:
        print(f"  {_format_tool_line(tool)}", flush=True)
    print("", flush=True)


def _format_cpu_frequency(mhz: float) -> str:
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def _cpu_frequency() -> str | None:
    cpu_freq = getattr(psutil, "cpu_freq", None)
    if not cpu_freq:
        return None
    try:
        frequency = cpu_freq()
    except Exception:
        return None
    return _format_cpu_frequency(frequency.current) if frequency else None


def _linux_cpu_model() -> str | None:
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return platform.processor() or None
    try:
        with open(cpuinfo, encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    _key, _sep, value = line.partition(":")
                    return value.strip() or None
    except OSError:
        return platform.processor() or None
    return platform.processor() or None


def _first_matching_line(output: str | None, prefix: str) -> str | None:
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            _key, _sep, value = stripped.partition(":")
            return value.strip() or None
    return None


def _first_value_line(output: str | None) -> str | None:
    if not output:
        return None
    values: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" in stripped:
            _key, _sep, value = stripped.partition("=")
            if value.strip():
                values.append(value.strip())
        elif not stripped.lower().startswith(
            ("model", "speed", "mediatype", "interfacetype")
        ):
            values.append(stripped)
    return ", ".join(values[:3]) if values else None


def _command_output(
    command: list[str], match: str | None = None, first_value: bool = False
) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if match:
        return _first_matching_line(output, match)
    if first_value:
        return _first_value_line(output)
    return output or None


def _cpu_model() -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _command_output(["sysctl", "-n", "machdep.cpu.brand_string"])
    if system == "Windows":
        return platform.processor() or _command_output(
            ["wmic", "cpu", "get", "Name", "/value"]
        )
    if system == "Linux":
        return _linux_cpu_model()
    return platform.processor() or None


def _ram_speed() -> str | None:
    system = platform.system()
    if system == "Darwin":
        speed = _command_output(["system_profiler", "SPMemoryDataType"])
        return _first_matching_line(speed, "Speed:")
    if system == "Windows":
        return _command_output(
            ["wmic", "memorychip", "get", "Speed", "/value"], first_value=True
        )
    if system == "Linux":
        return _command_output(["dmidecode", "-t", "memory"], match="Speed:")
    return None


def _linux_drive_model(path: Path) -> str | None:
    device = _command_output(["df", "--output=source", str(path)])
    if not device:
        return None
    lines = [line.strip() for line in device.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    block_name = Path(lines[-1]).name
    while block_name and not (Path("/sys/block") / block_name).exists():
        block_name = block_name[:-1]
    model_path = Path("/sys/block") / block_name / "device" / "model"
    if model_path.exists():
        try:
            return model_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def _filesystem_mount(path: Path) -> str:
    try:
        partitions = sorted(
            psutil.disk_partitions(all=True),
            key=lambda partition: len(partition.mountpoint),
            reverse=True,
        )
        resolved = str(path.resolve())
        for partition in partitions:
            if resolved == partition.mountpoint or resolved.startswith(
                partition.mountpoint.rstrip(os.sep) + os.sep
            ):
                return str(partition.mountpoint)
    except OSError:
        pass
    return str(path)
