"""`typing`: genes and repeats a VCF can't capture, typed from reads."""

import argparse

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
    cyp2d6.set_defaults(func=cmd_cyp2d6)

    hla = subs.add_parser("hla", parents=[base_parser], help=CLI_HELP["cmd_typing-hla"])
    hla.add_argument(
        "--hla-index",
        metavar="FASTA",
        help="T1K allele index (hla_dna_seq.fa). Default: <reference library>/"
        "ref/t1k_hla, built from IPD-IMGT/HLA on first use.",
    )
    hla.set_defaults(func=cmd_hla)

    repeats = subs.add_parser(
        "str", parents=[base_parser], help=CLI_HELP["cmd_typing-str"]
    )
    repeats.add_argument(
        "--catalog",
        metavar="JSON",
        help="ExpansionHunter variant catalog. Default: the one it ships for the "
        "input's build.",
    )
    repeats.add_argument(
        "--sex",
        choices=("male", "female"),
        help="Sample sex (the number of chrX alleles). Default: inferred from "
        "chrX/chrY coverage.",
    )
    repeats.set_defaults(func=cmd_str)
