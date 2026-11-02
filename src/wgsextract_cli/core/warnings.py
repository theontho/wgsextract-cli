import logging
import math
import shutil
from pathlib import Path

from wgsextract_cli.core.cram_counts import cram_read_counts
from wgsextract_cli.core.messages import SYSTEM_MESSAGES as MESSAGES
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.utils import run_command

GB = 1024**3

# A file's size alone misjudges sort and FASTQ space: CRAMs take from about 12 to
# 50 bytes per read. Measured on a 30x MGI CRAM (680M 149 bp reads, binned
# qualities, 12 bytes per read): samtools sort/collate temp files peaked at 74
# bytes per read and the gzipped FASTQ pair took 68.
TEMP_BYTES_PER_READ = 80
FASTQ_BYTES_PER_READ = 75


def warn_alignment_sort_space(path: PathInput, reads: int | None) -> None:
    """Warn about estimated sort spill space without adding a new hard limit."""
    if reads is None:
        return
    needed = max(0, reads) * TEMP_BYTES_PER_READ
    logging.info(
        "Alignment sort temporary disk estimate: %.2f GiB for %s input reads at %s.",
        needed / GB,
        f"{reads:,}",
        path,
    )
    check_free_space(path, math.ceil(needed / GB))


# Expected run times of various commands in seconds (from the WGS Extract GUI)
EXPECTED_TIME = {
    "GetBAMHeader": 2,
    "LiftoverCleanup": 5,
    "ButtonBAMStats": 15,
    "ButtonBAMStats2": 3 * 60,
    "ButtonMitoFASTA": 3 * 60,
    "ButtonMitoBAM": 3 * 60,
    "ButtonMitoVCF": 3 * 60,
    "ButtonYandMT": 3 * 60,
    "ButtonYonly": 3 * 60,
    "ButtonMicroarrayDNA": 5 * 60,
    "ButtonYHaplo2": 5 * 60,
    "ButtonBAMStatsLong": 10 * 60,
    "ButtonYHaplo": 10 * 60,
    "CoverageStatsPoz": 10 * 60,
    "AnnotatedVCF-yOnly": 10 * 60,
    "UnsortBAM": 10 * 60,
    "ButtonMTHaplo": 20 * 60,
    "GenLoadGenome": 20 * 60,
    "GenSortedBAM": 30 * 60,
    "GenBAMIndex": 30 * 60,
    "BAMtoCRAM": 30 * 60,
    "ExtractWES": 30 * 60,
    "ButtonBAMNoIndex": 35 * 60,
    "ButtonFastp": 45 * 60,
    "ButtonUnmappedReads": 45 * 60,
    "CoverageStats": 45 * 60,
    "ButtonCombinedKit": 50 * 60,
    "ButtonFastqc": 60 * 60,
    "CoverageStatsWES": 60 * 60,
    "AlignCleanup2": 60 * 60,
    "GenBAMSubset": 60 * 60,
    "ButtonBAMNoSort": 70 * 60,
    "ButtonUnalignBAM": 75 * 60,
    "CRAMtoBAM": 90 * 60,
    "CoverageStatsBIN": 120 * 60,
    "AlignCleanup": 120 * 60,
    "ButtonSNPVCF": 150 * 60,
    "ButtonInDelVCF": 150 * 60,
    "CreateAlignIndices": 180 * 60,
    "ButtonAlignBAM": 9 * 60 * 60,
}


def format_time(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    elif seconds < 3600:
        return f"{seconds // 60} minutes"
    else:
        return f"{seconds / 3600:.1f} hours"


def indexed_read_count(path: PathInput, threads: str | int = 1) -> int | None:
    """Exact total records in indexed BAM/CRAM, or None if unavailable."""
    # Without an index, idxstats silently decodes the whole file instead.
    if not alignment_index(path):
        return None
    logging.info(f"Counting reads in {path} to estimate the disk space needed...")
    if Path(path).suffix.lower() == ".cram":
        counts = cram_read_counts(path)
        if counts is not None:
            return sum(counts.values()) or None
    # CRAM idxstats decodes every container; more than 4 threads only slows it.
    threads = min(int(threads), 4)
    try:
        res = run_command(
            ["samtools", "idxstats", "-@", str(threads), path],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    rows = [line.split("\t") for line in res.stdout.splitlines()]
    return sum(int(r[2]) + int(r[3]) for r in rows if len(r) >= 4) or None


def get_free_space_needed(
    file_size_bytes: int,
    sort_type: str = "Coord",
    is_cram: bool = False,
    reads: int | None = None,
) -> tuple[int, int]:
    """Estimate (temp GB, final GB) needed to sort/convert a file of this size.

    ``reads`` (from `indexed_read_count`) raises the estimate for compact CRAMs;
    a "Name" final output is a gzipped FASTQ pair or unaligned BAM.
    """
    cram_mult = 1.95 if is_cram else 1.0
    name_mult = 1.45 if sort_type == "Name" else 1.0

    isize_gb = file_size_bytes * cram_mult / GB
    temp_needed_gb = isize_gb * name_mult + isize_gb
    final_needed_gb = isize_gb
    if reads:
        temp_needed_gb = max(temp_needed_gb, reads * TEMP_BYTES_PER_READ / GB)
        if sort_type == "Name":
            final_needed_gb = max(final_needed_gb, reads * FASTQ_BYTES_PER_READ / GB)

    return int(temp_needed_gb), int(final_needed_gb)


def print_warning(
    action_key: str,
    app_name: str | None = None,
    size_gb: int | None = None,
    final_gb: int | None = None,
    threads: str | None = None,
    file_size: int | None = None,
    is_cram: bool = False,
) -> None:
    """Prints a warning message and expected time for a given action."""

    # 1. Expected Time
    if action_key in EXPECTED_TIME:
        wait_time = EXPECTED_TIME[action_key]
        # Adjust for threads if it's a parallelizable action
        # This is a rough heuristic as per the original app's logic
        if threads and action_key in [
            "ButtonAlignBAM",
            "ButtonUnalignBAM",
            "GenSortedBAM",
            "ButtonSNPVCF",
            "ButtonInDelVCF",
        ]:
            try:
                t = int(threads)
                if t > 1:
                    # Very rough heuristic: speed up but not linearly
                    wait_time = wait_time / (t**0.7)
            except ValueError:
                pass

        time_str = format_time(int(wait_time))
        logging.warning(
            f"!!! WARNING: {MESSAGES['ExpectedWait'].format(time=time_str)} !!!"
        )

    # 2. Specific Messages
    if action_key == "infoFreeSpace":
        if file_size is not None:
            # Dynamic calculation
            sort_type = "Name" if app_name and "Name" in app_name else "Coord"
            calc_temp, calc_final = get_free_space_needed(file_size, sort_type, is_cram)
            size_gb = size_gb if size_gb is not None else calc_temp
            final_gb = final_gb if final_gb is not None else calc_final

        if app_name and size_gb is not None and final_gb is not None:
            msg = MESSAGES["infoFreeSpace"].format(
                app=app_name, size=size_gb, final=final_gb
            )
            logging.warning(f"!!! {msg} !!!")

    elif action_key == "RealignBAMTimeWarnMesg" and threads:
        try:
            cpus = int(threads)
            estimated_hours = 5 + 160 / cpus
            msg = MESSAGES["RealignBAMTimeWarnMesg"].format(
                time=f"{estimated_hours:.1f}"
            )
            logging.warning(f"!!! {msg} !!!")
        except (ValueError, ZeroDivisionError):
            pass

    elif action_key in MESSAGES:
        logging.warning(f"!!! {MESSAGES[action_key]} !!!")


def check_free_space(path: PathInput, required_gb: int) -> bool:
    """Helper to check if a path has enough free space."""
    if not Path(path).exists():
        return True  # Can't check if path doesn't exist yet

    total, used, free = shutil.disk_usage(path)
    free_gb = free / GB
    if free_gb < required_gb:
        logging.warning(f"!!! {MESSAGES['insufficient_disk_title']} !!!")
        msg = MESSAGES["insufficient_disk_msg"].format(
            detected=f"{free_gb:.1f}", path=path, needed=required_gb
        )
        for line in msg.split("\n"):
            logging.warning(f"!!! {line} !!!")
        return False
    return True
