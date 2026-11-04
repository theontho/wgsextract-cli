import argparse

from wgsextract_cli.api._cli import add_arguments
from wgsextract_cli.api.info import (
    CalculateCoverageParams,
    ChipParams,
    CoverageSampleParams,
    InfoParams,
)
from wgsextract_cli.core.messages import CLI_HELP

from . import chip
from .runner import (
    run,
    run_info,
)


def _inherit_format(parser: argparse.ArgumentParser) -> None:
    """Without its own --format, a coverage subcommand keeps the one given
    before it (`info --format parquet calculate-coverage`)."""
    for action in parser._actions:
        if action.dest == "table_format":
            action.default = argparse.SUPPRESS


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "info", parents=[base_parser], help=CLI_HELP["cmd_info"]
    )
    add_arguments(parser, InfoParams)
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
    add_arguments(calc_cov, CalculateCoverageParams)
    _inherit_format(calc_cov)
    calc_cov.set_defaults(func=run)

    samp_cov = info_subs.add_parser(
        "coverage-sample", parents=[base_parser], help=CLI_HELP["cmd_coverage-sample"]
    )
    add_arguments(samp_cov, CoverageSampleParams)
    _inherit_format(samp_cov)
    samp_cov.set_defaults(func=run)

    chip_parser = info_subs.add_parser(
        "chip", parents=[base_parser], help=CLI_HELP["cmd_info-chip"]
    )
    add_arguments(chip_parser, ChipParams)
    chip_parser.set_defaults(func=chip.run)

    parser.set_defaults(func=run)


__all__ = ["register", "run", "run_info"]
