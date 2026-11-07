import argparse

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.deps import DepsCheckParams
from wgsextract_cli.core.messages import CLI_HELP

from ._deps_status import run


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    deps_parser = subparsers.add_parser("deps", help=CLI_HELP["cmd_deps"])
    deps_subparsers = deps_parser.add_subparsers(dest="subcommand", required=True)

    check_parser = deps_subparsers.add_parser(
        "check", parents=[base_parser], help=CLI_HELP["cmd_check-deps"]
    )
    add_arguments(check_parser, DepsCheckParams)
    check_parser.set_defaults(func=cli_handler(DepsCheckParams, run, debug="debug"))
