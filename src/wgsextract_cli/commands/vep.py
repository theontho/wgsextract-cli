import argparse

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.vep import VepDownloadParams, VepRunParams, VepVerifyParams
from wgsextract_cli.core.messages import CLI_HELP

from ._vep_resources import run_vep_download, run_vep_verify
from ._vep_run import run_vep

cli_vep_run = cli_handler(VepRunParams, run_vep)
cli_vep_download = cli_handler(VepDownloadParams, run_vep_download)
cli_vep_verify = cli_handler(VepVerifyParams, run_vep_verify)


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    add_arguments(parser, VepRunParams, config_defaults=True)
    parser.set_defaults(func=cli_vep_run)


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
    add_arguments(dl_parser, VepDownloadParams, config_defaults=True)
    dl_parser.set_defaults(func=cli_vep_download)

    verify_parser = vep_subs.add_parser(
        "verify", parents=[base_parser], help=CLI_HELP["cmd_vep-verify"]
    )
    add_arguments(verify_parser, VepVerifyParams, config_defaults=True)
    verify_parser.set_defaults(func=cli_vep_verify)

    run_parser = vep_subs.add_parser(
        "run", parents=[base_parser], help=CLI_HELP["cmd_vep-run"]
    )
    _add_run_args(run_parser)
