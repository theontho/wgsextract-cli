#!/usr/bin/env python3

import logging
import sys
from collections.abc import Sequence

from .cli.bootstrap import (
    configure_logging,
    configure_stdio_encoding,
    install_signal_handlers,
    start_parent_monitor,
)
from .cli.dispatch import dispatch_command
from .cli.parser import build_parser, config_path_from_argv, parse_args, print_full_help
from .core.config import reload_settings, set_config_path
from .core.run_log import RunLogError, run_logging

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> None:
    configure_stdio_encoding()
    set_config_path(config_path_from_argv(sys.argv[1:] if argv is None else argv))
    reload_settings()

    parser, base_parser = build_parser()
    args, explicit_dests = parse_args(parser, base_parser, argv)

    if args.full_help:
        print_full_help(parser)
        raise SystemExit(0)

    configure_logging(debug=args.debug, quiet=args.quiet)
    install_signal_handlers()
    start_parent_monitor(args.parent_pid)

    try:
        with run_logging(args, sys.argv[1:] if argv is None else argv):
            if hasattr(args, "func"):
                dispatch_command(args, explicit_dests)
            else:
                parser.print_help()
    except RunLogError as exc:
        logging.error("Cannot write persistent run log: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
