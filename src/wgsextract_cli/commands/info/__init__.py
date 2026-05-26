import argparse

from wgsextract_cli.core.messages import CLI_HELP

from .runner import (
    run,
)


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "info", parents=[base_parser], help=CLI_HELP["cmd_info"]
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help=CLI_HELP["arg_detailed"],
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help=CLI_HELP["arg_csv"],
    )

    info_subs = parser.add_subparsers(dest="info_cmd", required=False)
    calc_cov = info_subs.add_parser(
        "calculate-coverage",
        parents=[base_parser],
        help=CLI_HELP["cmd_calculate-coverage"],
    )
    calc_cov.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    calc_cov.set_defaults(func=run)

    samp_cov = info_subs.add_parser(
        "coverage-sample", parents=[base_parser], help=CLI_HELP["cmd_coverage-sample"]
    )
    samp_cov.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    samp_cov.set_defaults(func=run)

    parser.set_defaults(func=run)
