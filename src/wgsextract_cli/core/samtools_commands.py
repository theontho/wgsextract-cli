import platform
import shutil


def get_sam_sort_cmd(
    out_file: str,
    threads: str,
    memory: str,
    fmt: str = "BAM",
    reference: str | None = None,
    name_sort: bool = False,
    temp_dir: str | None = None,
) -> list[str]:
    """
    Return a command list for sorting BAM/CRAM.
    Uses sambamba if available (except on macOS) and format is BAM, else samtools.
    """
    threads_val = int(threads)
    mem_val = int(memory.rstrip("GgMm"))
    is_gb = memory.lower().endswith("g")
    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and fmt == "BAM" and not is_macos:
        total_mem = mem_val * threads_val
        total_mem_str = f"{total_mem}G" if is_gb else f"{total_mem}M"
        cmd = [
            "sambamba",
            "sort",
            "-t",
            threads,
            "-m",
            total_mem_str,
            "-o",
            out_file,
            "/dev/stdin",
        ]
        if name_sort:
            cmd.insert(2, "-n")
        if temp_dir:
            cmd.insert(2, "--tmpdir")
            cmd.insert(3, temp_dir)
        return cmd

    cmd = ["samtools", "sort", "-@", threads, "-m", memory, "-o", out_file]
    if name_sort:
        cmd.append("-n")
    if temp_dir:
        cmd += ["-T", temp_dir]
    if fmt == "CRAM":
        cmd += ["-O", "CRAM"]
        if reference:
            cmd += ["--reference", reference]
    elif fmt == "SAM":
        cmd += ["-O", "SAM"]
    else:
        cmd += ["-O", "BAM"]
    return cmd


def get_sam_index_cmd(file_path: str, threads: str = "1") -> list[str]:
    """
    Return a command list for indexing BAM/CRAM.
    Uses sambamba if available (except on macOS) and file is BAM, else samtools.
    """
    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and file_path.lower().endswith(".bam") and not is_macos:
        return ["sambamba", "index", "-t", threads, file_path]
    return ["samtools", "index", file_path]


def get_sam_view_cmd(
    threads: str = "1",
    fmt: str = "BAM",
    reference: str | None = None,
    is_input_sam: bool = False,
) -> list[str]:
    """
    Return a command list for viewing/converting BAM/CRAM.
    Uses sambamba if available (except on macOS) and fmt is BAM, else samtools.
    """
    is_macos = platform.system() == "Darwin"

    if shutil.which("sambamba") and fmt == "BAM" and not reference and not is_macos:
        cmd = ["sambamba", "view", "-t", threads, "-f", "bam"]
        if is_input_sam:
            cmd += ["-S"]
        return cmd

    cmd = ["samtools", "view", "-@", threads]
    if fmt == "CRAM":
        cmd += ["-O", "CRAM"]
        if reference:
            cmd += ["-T", reference]
    elif fmt == "BAM":
        cmd += ["-b"]

    return cmd
