"""`typing`: genes and repeats a VCF can't capture, typed from reads."""

import argparse

from wgsextract_cli.api._cli import add_arguments
from wgsextract_cli.api.genotyping import (
    TypingCyp2d6Params,
    TypingHlaParams,
    TypingStrParams,
)
from wgsextract_cli.core.messages import CLI_HELP

from .cyp2d6 import cmd_cyp2d6
from .hla import cmd_hla
from .repeats import cmd_str


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser("typing", help=CLI_HELP["cmd_typing"])
    subs = parser.add_subparsers(dest="typing_cmd", required=True)

    cyp2d6 = subs.add_parser(
        "cyp2d6", parents=[base_parser], help=CLI_HELP["cmd_typing-cyp2d6"]
    )
    add_arguments(cyp2d6, TypingCyp2d6Params)
    cyp2d6.set_defaults(func=cmd_cyp2d6)

    hla = subs.add_parser("hla", parents=[base_parser], help=CLI_HELP["cmd_typing-hla"])
    add_arguments(hla, TypingHlaParams)
    hla.set_defaults(func=cmd_hla)

    repeats = subs.add_parser(
        "str", parents=[base_parser], help=CLI_HELP["cmd_typing-str"]
    )
    add_arguments(repeats, TypingStrParams)
    repeats.set_defaults(func=cmd_str)
