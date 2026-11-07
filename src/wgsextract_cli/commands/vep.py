import argparse

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.vep import VepRunParams
from wgsextract_cli.core.messages import CLI_HELP

from ._vep_resources import (
    VEP_MIRRORS,
    cmd_vep_download,
    cmd_vep_verify,
)
from ._vep_run import run_vep

cli_vep_run = cli_handler(VepRunParams, run_vep)


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    add_arguments(parser, VepRunParams, config_defaults=True)
    parser.set_defaults(func=cli_vep_run)


def _add_cache_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--species", default="homo_sapiens", help="Species name (default: homo_sapiens)"
    )
    parser.add_argument(
        "--assembly",
        choices=["GRCh37", "GRCh38"],
        default="GRCh38",
        help="Assembly (default: GRCh38)",
    )
    parser.add_argument(
        "--vep-version", default="116", help="Ensembl release version (default: 116)"
    )
    parser.add_argument("--vep-cache", help="Path to VEP cache directory")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "vep", parents=[base_parser], help=CLI_HELP["cmd_vep"]
    )
    # `wgsextract vep ...` and `wgsextract vep run ...` both run VEP.
    _add_run_args(parser)
    vep_subs = parser.add_subparsers(dest="vep_cmd", required=False)

    dl_parser = vep_subs.add_parser(
        "download", parents=[base_parser], help=CLI_HELP["cmd_vep-download"]
    )
    _add_cache_args(dl_parser)
    dl_parser.add_argument(
        "--mirror",
        choices=sorted(VEP_MIRRORS),
        default="uk",
        help="Ensembl FTP mirror to use (default: uk)",
    )
    dl_parser.set_defaults(func=cmd_vep_download)

    verify_parser = vep_subs.add_parser(
        "verify", parents=[base_parser], help=CLI_HELP["cmd_vep-verify"]
    )
    _add_cache_args(verify_parser)
    verify_parser.set_defaults(func=cmd_vep_verify)

    run_parser = vep_subs.add_parser(
        "run", parents=[base_parser], help=CLI_HELP["cmd_vep-run"]
    )
    _add_run_args(run_parser)
