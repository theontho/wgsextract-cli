import argparse
import logging
import sys
from pathlib import Path

from wgsextract_cli.core.genome_library import apply_genome_selection
from wgsextract_cli.core.keep_awake import keep_awake
from wgsextract_cli.core.utils import WGSExtractError

from .platform_support import verify_platform_support

# Commands that only report: they mustn't create the configured output folder
# (it may be on a drive that isn't mounted).
READ_ONLY_COMMANDS = frozenset({"help", "config", "deps"})


def dispatch_command(args: argparse.Namespace, explicit_dests: set[str]) -> None:
    if not hasattr(args, "func"):
        raise RuntimeError("No command function is registered for parsed arguments.")

    try:
        verify_platform_support(args)
        apply_genome_selection(args, set(explicit_dests))
        input_path = getattr(args, "input", None)
        if (
            input_path
            and "input" in explicit_dests
            and getattr(args, "command", None)
            not in {*READ_ONLY_COMMANDS, "ref", "benchmark", "sign"}
            and getattr(args, "qc_cmd", None) != "fake-data"
            and Path(input_path).suffix.lower() in {".bam", ".cram"}
        ):
            from wgsextract_cli.core.alignment_input import validate_alignment_input

            validate_alignment_input(input_path)
        if args.outdir and getattr(args, "command", None) not in READ_ONLY_COMMANDS:
            Path(args.outdir).mkdir(parents=True, exist_ok=True)
        from wgsextract_cli.core.atomic_output import atomic_output_mode

        with keep_awake(
            bool(getattr(args, "keep_awake", False))
            and getattr(args, "command", None) not in READ_ONLY_COMMANDS
        ):
            with atomic_output_mode(bool(getattr(args, "atomic_output", False))):
                args.func(args)
    except WGSExtractError as e:
        logging.error(str(e))
        sys.exit(1)
    except Exception as e:
        if args.debug:
            logging.exception("An unexpected error occurred:")
        else:
            logging.error(f"An unexpected error occurred: {e}")
        sys.exit(1)
