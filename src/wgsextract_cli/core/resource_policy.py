from collections.abc import Callable

import psutil

from wgsextract_cli.core.runtime import (
    ThreadTuningProfile,
    default_thread_tuning_profile,
)


def parse_thread_count(value: object) -> int | None:
    """Parse a --threads / cpu_threads value; None means "auto"."""
    if value is None or (isinstance(value, str) and value.strip().lower() == "auto"):
        return None
    try:
        count = int(str(value).strip())
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"expected a positive integer or 'auto', got {value!r}")
    return count


def get_resource_defaults(
    threads_arg: int | str | None = None,
    memory_arg: str | None = None,
    thread_profile_factory: Callable[
        [], ThreadTuningProfile
    ] = default_thread_tuning_profile,
) -> tuple[str, str]:
    """
    Calculate default CPU threads and memory if not provided.
    Memory defaults to 25% of RAM (at least 2 GB) split across threads.
    """
    if threads_arg is None or str(threads_arg).strip().lower() == "auto":
        threads_count = int(thread_profile_factory().threads)
    else:
        threads_count = int(threads_arg)
    threads_count = max(1, threads_count)
    threads = str(threads_count)

    if memory_arg is not None and str(memory_arg).strip().lower() != "auto":
        memory = str(memory_arg)
    else:
        total_mem_gb = psutil.virtual_memory().total / (1024**3)
        safe_mem = max(2, int(total_mem_gb * 0.25))
        mem_per_thread = max(1, safe_mem // threads_count)
        memory = f"{mem_per_thread}G"

    return threads, memory
