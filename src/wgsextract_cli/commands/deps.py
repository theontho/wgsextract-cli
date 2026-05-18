from typing import Any

from wgsextract_cli.core import (
    runtime_paths,
)
from wgsextract_cli.core.messages import CLI_HELP

from ._deps_runtime import (
    run_bundled_runtime_check,
    run_bundled_runtime_setup,
    run_pacman_check,
)
from ._deps_status import (
    DEFAULT_WINDOWS_RUNTIME_RELEASE_URL,
    run,
    run_wsl_check,
    run_wsl_tune,
)


def register(subparsers: Any, base_parser: Any) -> None:
    deps_parser = subparsers.add_parser(
        "deps", parents=[base_parser], help=CLI_HELP["cmd_deps"]
    )
    deps_subparsers = deps_parser.add_subparsers(dest="subcommand", required=True)

    check_parser = deps_subparsers.add_parser(
        "check", parents=[base_parser], help=CLI_HELP["cmd_check-deps"]
    )
    check_parser.add_argument(
        "--tool", help="Check for a specific tool and return exit code 1 if missing."
    )
    check_parser.set_defaults(func=run)

    wsl_parser = deps_subparsers.add_parser(
        "wsl", parents=[base_parser], help="Check or tune the Windows WSL runtime."
    )
    wsl_subparsers = wsl_parser.add_subparsers(dest="wsl_command", required=True)

    wsl_check_parser = wsl_subparsers.add_parser(
        "check", help="Check WSL runtime availability."
    )
    wsl_check_parser.set_defaults(func=run_wsl_check)

    wsl_tune_parser = wsl_subparsers.add_parser(
        "tune",
        help="Write WSL2 resource settings. Uses host-based defaults if omitted.",
    )
    wsl_tune_parser.add_argument(
        "--memory", help="WSL memory limit, such as 24GB. Defaults to 75%% of RAM."
    )
    wsl_tune_parser.add_argument(
        "--processors",
        type=int,
        help="Number of CPU processors available to WSL. Defaults to 2/3 of logical CPUs.",
    )
    wsl_tune_parser.add_argument(
        "--swap", help="WSL swap size, such as 16GB. Defaults to 25%% of RAM."
    )
    wsl_tune_parser.set_defaults(func=run_wsl_tune)

    for mode in ("cygwin", "msys2"):
        spec = runtime_paths.bundled_runtime_spec(mode)
        bundled_parser = deps_subparsers.add_parser(
            mode,
            parents=[base_parser],
            help=f"Set up or check the bundled Windows {spec.display_name} runtime.",
        )
        bundled_subparsers = bundled_parser.add_subparsers(
            dest=f"{mode}_command", required=True
        )

        bundled_check_parser = bundled_subparsers.add_parser(
            "check", help=f"Check {spec.display_name} runtime availability."
        )
        bundled_check_parser.set_defaults(func=run_bundled_runtime_check, mode=mode)

        bundled_setup_parser = bundled_subparsers.add_parser(
            "setup", help=f"Download and extract {spec.display_name} into runtime/."
        )
        bundled_setup_parser.add_argument(
            "--url",
            help="Direct runtime ZIP URL. Overrides the release JSON lookup.",
        )
        bundled_setup_parser.add_argument(
            "--latest-json-url",
            default=DEFAULT_WINDOWS_RUNTIME_RELEASE_URL,
            help="Release JSON URL containing cygwin64/msys2 package URLs.",
        )
        bundled_setup_parser.add_argument(
            "--cache-dir",
            help="Directory used to cache downloaded runtime ZIP packages.",
        )
        bundled_setup_parser.add_argument(
            "--archive-dir",
            help="Directory containing pre-downloaded runtime ZIP packages.",
        )
        bundled_setup_parser.add_argument(
            "--source-dir",
            help=(
                "Directory containing an already-built runtime tree to copy. "
                "May be the runtime directory itself or a parent containing "
                f"{spec.dirname}/."
            ),
        )
        bundled_setup_parser.add_argument(
            "--refresh-download",
            action="store_true",
            help="Re-download the runtime ZIP even when it already exists in the cache.",
        )
        bundled_setup_parser.add_argument(
            "--force",
            action="store_true",
            help="Replace the existing extracted runtime directory if present.",
        )
        bundled_setup_parser.set_defaults(func=run_bundled_runtime_setup, mode=mode)

    pacman_parser = deps_subparsers.add_parser(
        "pacman",
        parents=[base_parser],
        help="Check native Windows bio tools installed by MSYS2 UCRT64 pacman.",
    )
    pacman_subparsers = pacman_parser.add_subparsers(
        dest="pacman_command", required=True
    )
    pacman_check_parser = pacman_subparsers.add_parser(
        "check", help="Check MSYS2 UCRT64 pacman-installed tool availability."
    )
    pacman_check_parser.set_defaults(func=run_pacman_check)
