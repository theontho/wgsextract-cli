import argparse

from wgsextract_cli.core.messages import CLI_HELP

from ._deps_status import run


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    deps_parser = subparsers.add_parser("deps", help=CLI_HELP["cmd_deps"])
    deps_subparsers = deps_parser.add_subparsers(dest="subcommand", required=True)

    check_parser = deps_subparsers.add_parser(
        "check", parents=[base_parser], help=CLI_HELP["cmd_check-deps"]
    )
    check_parser.add_argument(
        "--tool", help="Check for a specific tool and return exit code 1 if missing."
    )
    check_parser.set_defaults(func=run)
