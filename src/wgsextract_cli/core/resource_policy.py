from collections.abc import Callable

from wgsextract_cli.core.runtime import (
    ThreadTuningProfile,
    default_thread_tuning_profile,
)

try:
    import psutil
except ImportError:
    psutil = None


def get_resource_defaults(
    threads_arg: int | str | None = None,
    memory_arg: str | None = None,
    thread_profile_factory: Callable[
        [], ThreadTuningProfile
    ] = default_thread_tuning_profile,
) -> tuple[str, str]:
    """
    Calculate default CPU threads and memory if not provided.
    Logic inspired by Adjust_Mem in program/mainwindow.py.
    """
    if threads_arg is not None:
        threads = str(threads_arg)
    else:
        threads = str(thread_profile_factory().threads)

    if memory_arg is not None:
        memory = str(memory_arg)
    elif psutil:
        total_mem_gb = psutil.virtual_memory().total / (1024**3)
        safe_mem = max(2, int(total_mem_gb * 0.25))
        mem_per_thread = max(1, safe_mem // int(threads))
        memory = f"{mem_per_thread}G"
    else:
        memory = "1G"

    return threads, memory
