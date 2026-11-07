"""``benchmark``: the CLI command; ``api.benchmark`` documents the operation."""

import argparse

from wgsextract_cli.core.messages import CLI_HELP


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    # Imported here: api.benchmark imports this package's models.
    from wgsextract_cli.api._cli import add_arguments, cli_handler
    from wgsextract_cli.api.benchmark import BenchmarkParams

    from .runner import run_benchmark

    parser = subparsers.add_parser(
        "benchmark",
        parents=[base_parser],
        help=CLI_HELP["cmd_benchmark"],
        formatter_class=argparse.RawTextHelpFormatter,
    )
    add_arguments(parser, BenchmarkParams)
    handler = cli_handler(
        BenchmarkParams,
        run_benchmark,
        debug="debug",
        quiet="quiet",
        keep_awake="keep_awake",
    )

    def run(args: argparse.Namespace) -> None:
        # Benchmarks save under out/benchmark unless --outdir is given: config
        # output_directory doesn't apply.
        if "outdir" not in getattr(args, "_explicit_dests", set()):
            args.outdir = None
        handler(args)

    run.__module__, run.__name__, run.__qualname__ = (
        handler.__module__,
        handler.__name__,
        handler.__qualname__,
    )
    parser.set_defaults(func=run)
