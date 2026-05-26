import argparse
import logging
import os
import sys

from wgsextract_cli.core.genome_library import apply_genome_selection
from wgsextract_cli.core.utils import WGSExtractError


def dispatch_command(args: argparse.Namespace, explicit_dests: set[str]) -> None:
    if not hasattr(args, "func"):
        raise RuntimeError("No command function is registered for parsed arguments.")

    try:
        apply_genome_selection(args, set(explicit_dests))
        if args.outdir:
            os.makedirs(args.outdir, exist_ok=True)
        args.func(args)
    except WGSExtractError as e:
        logging.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        if args.debug:
            logging.exception("An unexpected error occurred:")
        else:
            logging.error(f"An unexpected error occurred: {e}")
        sys.exit(1)
