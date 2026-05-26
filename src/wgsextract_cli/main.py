#!/usr/bin/env python3

import os
from collections.abc import Sequence

from .cli.bootstrap import (
    _parent_process_is_alive,
    configure_logging,
    configure_stdio_encoding,
    describe_signal,
    install_signal_handlers,
    start_parent_monitor,
)
from .cli.dispatch import dispatch_command
from .cli.parser import build_parser, parse_args, print_full_help
from .core.config import reload_settings

__all__ = ["_parent_process_is_alive", "describe_signal", "main", "os"]


def main(argv: Sequence[str] | None = None) -> None:
    configure_stdio_encoding()
    reload_settings()

    parser, base_parser = build_parser()
    args, explicit_dests = parse_args(parser, base_parser, argv)

    if args.full_help:
        print_full_help(parser)
        raise SystemExit(0)

    configure_logging(debug=args.debug, quiet=args.quiet)
    install_signal_handlers()
    start_parent_monitor(args.parent_pid)

    if hasattr(args, "func"):
        dispatch_command(args, explicit_dests)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
