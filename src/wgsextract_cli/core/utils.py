import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import IO

from wgsextract_cli.core.process_registry import (
    cleanup_processes as cleanup_processes,
)
from wgsextract_cli.core.process_registry import (
    proc_registry,
    process_group_kwargs,
)
from wgsextract_cli.core.resource_policy import (
    get_resource_defaults as _get_resource_defaults,
)
from wgsextract_cli.core.runtime import (
    default_thread_tuning_profile as default_thread_tuning_profile,
)
from wgsextract_cli.core.samtools_commands import (
    get_sam_index_cmd as get_sam_index_cmd,
)
from wgsextract_cli.core.samtools_commands import (
    get_sam_sort_cmd as get_sam_sort_cmd,
)
from wgsextract_cli.core.samtools_commands import (
    get_sam_view_cmd as get_sam_view_cmd,
)

_process_group_kwargs = process_group_kwargs


class WGSExtractError(Exception):
    """Base exception for wgsextract-cli errors."""


def get_resource_defaults(
    threads_arg: int | str | None = None, memory_arg: str | None = None
) -> tuple[str, str]:
    """Calculate default CPU threads and memory if not provided."""
    return _get_resource_defaults(
        threads_arg, memory_arg, default_thread_tuning_profile
    )


def _normalize_subprocess_cmd(cmd: str | Sequence[object]) -> list[str]:
    """Expand shell-style command strings and configured Pixi tool wrappers."""
    from wgsextract_cli.core import (
        runtime,
        runtime_wrappers,
    )

    def split_wrapper_or_keep(value: str) -> list[str]:
        if (
            runtime.is_wsl_tool_command(value)
            or runtime.is_bundled_tool_command(value)
            or runtime.is_pacman_tool_command(value)
        ):
            return [value]
        if os.path.exists(value):
            return [value]
        if "pixi run" in value or " " in value:
            return shlex.split(value)
        return [value]

    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd)
    else:
        cmd_list = []
        for index, item in enumerate(cmd):
            if index == 0 and isinstance(item, str):
                cmd_list.extend(split_wrapper_or_keep(item))
            else:
                cmd_list.append(str(item))

    if cmd_list and isinstance(cmd_list[0], str):
        executable = cmd_list[0]
        if runtime.is_wsl_tool_command(executable):
            return runtime_wrappers.wrap_command(cmd_list)

        if executable and " " in executable and not os.path.exists(executable):
            cmd_list = shlex.split(executable) + cmd_list[1:]
            executable = cmd_list[0]

        if (
            executable
            and os.path.basename(executable) == executable
            and shutil.which(executable) is None
        ):
            from wgsextract_cli.core.dependencies import get_tool_path

            resolved = get_tool_path(executable)
            if resolved:
                if (
                    runtime.is_wsl_tool_command(resolved)
                    or runtime.is_bundled_tool_command(resolved)
                    or runtime.is_pacman_tool_command(resolved)
                ):
                    cmd_list = [resolved] + cmd_list[1:]
                elif os.path.exists(resolved):
                    cmd_list = [resolved] + cmd_list[1:]
                else:
                    cmd_list = shlex.split(resolved) + cmd_list[1:]

    return runtime_wrappers.wrap_command(cmd_list)


def run_command(
    cmd: str | Sequence[object],
    capture_output: bool = False,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: IO[bytes] | IO[str] | int | None = None,
    stdout: IO[bytes] | IO[str] | int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Helper to run subprocess with logging and registry."""
    cmd_list = _normalize_subprocess_cmd(cmd)

    cmd_str = " ".join(cmd_list)
    logging.debug(f"Running: {cmd_str}")

    proc_stdout = (
        stdout if stdout is not None else (subprocess.PIPE if capture_output else None)
    )
    proc_stderr = subprocess.PIPE if capture_output else None

    process = subprocess.Popen(
        cmd_list,
        stdout=proc_stdout,
        stderr=proc_stderr,
        stdin=stdin,
        text=True if capture_output or (stdout is None) else False,
        env=env,
        **process_group_kwargs(),
    )

    proc_registry.register_process(cmd_str, process)
    try:
        res_stdout, res_stderr = process.communicate()
        if check and process.returncode != 0:
            logging.error(f"Command failed: {cmd_str}")
            if res_stderr:
                logging.error(res_stderr)
            raise subprocess.CalledProcessError(
                process.returncode, cmd_list, res_stdout, res_stderr
            )
        return subprocess.CompletedProcess(
            cmd_list, process.returncode, res_stdout, res_stderr
        )
    finally:
        proc_registry.unregister_process(cmd_str)
