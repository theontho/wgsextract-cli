import platform
import re
from os import fspath
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.paths import PathInput, append_suffix


def alignment_index(path: PathInput) -> str | None:
    """The index of a BAM/CRAM (x.bam.bai, x.bai, x.cram.crai, ...), if any."""
    file = Path(path)
    candidates = [append_suffix(file, ext) for ext in (".bai", ".csi", ".crai")]
    candidates += [file.with_suffix(".bai"), file.with_suffix(".crai")]
    return next((str(index) for index in candidates if index.exists()), None)


def parse_memory(memory: str) -> tuple[float, str]:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([GgMm])(?:[Bb])?\s*", memory)
    if not match:
        raise ValueError(f"Invalid memory value: {memory}")
    return float(match.group(1)), match.group(2).upper()


def _format_memory(amount: float, unit: str) -> str:
    return f"{amount:g}{unit}"


def _use_sambamba() -> bool:
    """sambamba is preferred where installed, except on macOS."""
    return platform.system() != "Darwin" and get_tool_path("sambamba") is not None


def get_sam_sort_cmd(
    out_file: PathInput,
    threads: str,
    memory: str,
    fmt: str = "BAM",
    reference: PathInput | None = None,
    name_sort: bool = False,
    temp_dir: PathInput | None = None,
    sam_input: bool = False,
) -> list[str]:
    """
    Return a command list for sorting BAM/CRAM read from stdin.
    Uses sambamba for BAM output when available, else samtools. sambamba sort
    cannot read SAM, so pass sam_input=True for aligner output streams.
    """
    threads_val = int(threads)
    mem_val, mem_unit = parse_memory(memory)

    if fmt == "BAM" and not sam_input and _use_sambamba():
        total_mem = mem_val * threads_val
        total_mem_str = _format_memory(total_mem, mem_unit)
        cmd = [
            "sambamba",
            "sort",
            "-t",
            threads,
            "-m",
            total_mem_str,
            "-o",
            fspath(out_file),
            "/dev/stdin",
        ]
        if name_sort:
            cmd.insert(2, "-n")
        if temp_dir:
            cmd.insert(2, "--tmpdir")
            cmd.insert(3, fspath(temp_dir))
        return cmd

    cmd = ["samtools", "sort", "-@", threads, "-m", memory, "-o", fspath(out_file)]
    if name_sort:
        cmd.append("-n")
    if temp_dir:
        cmd += ["-T", fspath(temp_dir)]
    if fmt == "CRAM":
        cmd += ["-O", "CRAM"]
        if reference:
            cmd += ["--reference", fspath(reference)]
    elif fmt == "SAM":
        cmd += ["-O", "SAM"]
    else:
        cmd += ["-O", "BAM"]
    return cmd


def get_sam_index_cmd(file_path: PathInput, threads: str = "1") -> list[str]:
    """
    Return a command list for indexing BAM/CRAM.
    Uses sambamba if available (except on macOS) and file is BAM, else samtools.
    """
    file = fspath(file_path)
    if file.lower().endswith(".bam") and _use_sambamba():
        return ["sambamba", "index", "-t", threads, file]
    return ["samtools", "index", file]


def get_sam_view_cmd(
    threads: str = "1",
    fmt: str = "BAM",
    reference: PathInput | None = None,
    is_input_sam: bool = False,
) -> list[str]:
    """
    Return a command list for viewing/converting BAM/CRAM.
    Uses sambamba if available (except on macOS) and fmt is BAM, else samtools.
    """
    if fmt == "BAM" and not reference and _use_sambamba():
        cmd = ["sambamba", "view", "-t", threads, "-f", "bam"]
        if is_input_sam:
            cmd += ["-S"]
        return cmd

    cmd = ["samtools", "view", "-@", threads]
    if fmt == "CRAM":
        cmd += ["-O", "CRAM"]
        if reference:
            cmd += ["-T", fspath(reference)]
    elif fmt == "BAM":
        cmd += ["-b"]

    return cmd
