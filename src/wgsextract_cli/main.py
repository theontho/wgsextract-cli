#!/usr/bin/env python3

import argparse
import logging
import os
import sys

from .commands import (
    align,
    analyze,
    bam,
    benchmark,
    deps,
    examples,
    extract,
    info,
    lineage,
    microarray,
    pet,
    qc,
    realign,
    ref,
    repair,
    vcf,
    vep,
)
from .core.config import KNOWN_SETTINGS, get_config_path, reload_settings, settings
from .core.genome_library import apply_genome_selection
from .core.messages import CLI_HELP
from .core.utils import WGSExtractError, cleanup_processes


def _parent_process_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False

    try:
        import psutil

        return bool(psutil.pid_exists(parent_pid))
    except ImportError:
        pass

    if sys.platform == "win32":
        return True

    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class EmojiFormatter(logging.Formatter):
    """Custom formatter to add emojis to log levels."""

    LEVEL_EMOJIS = {
        logging.DEBUG: "🔍",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❌",
        logging.CRITICAL: "🚨",
    }

    def format(self, record):
        level_fmt = self.LEVEL_EMOJIS.get(record.levelno, record.levelname)
        record.levelname = level_fmt
        return super().format(record)


def print_full_help(parser):
    """Print a concise tree of all commands and subcommands."""
    print("\n" + CLI_HELP["description"])
    print("\nGLOBAL OPTIONS:")
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction) and action.dest != "help":
            opts = ", ".join(action.option_strings)
            if opts:
                print(f"  {opts:<25} {action.help}")

    print("\nCOMMAND TREE:")
    _print_tree_recursive(parser, 0)


def _print_tree_recursive(parser, indent):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Map command names to their help text from the parent's perspective
            name_to_help = {}
            for choice_action in action._choices_actions:
                name_to_help[choice_action.dest] = choice_action.help

            for name in sorted(action.choices.keys()):
                subparser = action.choices[name]
                help_text = name_to_help.get(name, "")
                if not help_text and subparser.description:
                    help_text = subparser.description.split("\n")[0]

                indent_str = "  " * indent
                print(f"{indent_str}- {name:<20} {help_text}")
                _print_tree_recursive(subparser, indent + 1)


def _find_subcommand_index(
    argv: list[str],
    command_names: set[str],
    option_map: dict[str, argparse.Action],
) -> int | None:
    """Return the index of the first real subcommand in argv."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in command_names:
            return i

        option_name = token.split("=", 1)[0]
        action = option_map.get(option_name)
        if action is None:
            i += 1
            continue

        if action.nargs == 0 or "=" in token:
            i += 1
        else:
            i += 2

    return None


def _extract_shared_options(
    argv: list[str],
    option_map: dict[str, argparse.Action],
) -> dict[str, object]:
    values: dict[str, object] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        option_name, has_inline_value, inline_value = token.partition("=")
        action = option_map.get(option_name)
        if action is None:
            i += 1
            continue

        if action.nargs == 0:
            values[action.dest] = True
            i += 1
            continue

        raw_value = inline_value if has_inline_value else argv[i + 1]
        values[action.dest] = (
            action.type(raw_value) if callable(action.type) else raw_value
        )
        i += 1 if has_inline_value else 2

    return values


def _configure_stdio_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


def main():
    _configure_stdio_encoding()
    # Re-load config to pick up any environment changes made before calling main (useful for tests)
    reload_settings()

    handler = logging.StreamHandler()
    handler.setFormatter(
        EmojiFormatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    )
    # Use force=True to ensure our handler replaces any existing ones
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # 1. Create a parent parser for shared arguments.
    # Suppressed defaults prevent a subparser from overwriting values that were
    # supplied before the subcommand (for example: --input sample.bam info).
    base_parser = argparse.ArgumentParser(
        add_help=False, argument_default=argparse.SUPPRESS
    )
    base_parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help=CLI_HELP["arg_debug"],
    )
    base_parser.add_argument(
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress all informational logs.",
    )
    base_parser.add_argument(
        "--input",
        "-i",
        default=argparse.SUPPRESS,
        help=CLI_HELP["arg_input"],
    )
    base_parser.add_argument(
        "--outdir",
        "-o",
        default=argparse.SUPPRESS,
        help=CLI_HELP["arg_outdir"],
    )
    base_parser.add_argument(
        "--genome",
        default=argparse.SUPPRESS,
        help="Genome ID from genome_library. Uses that folder for inputs and outputs.",
    )
    base_parser.add_argument(
        "--ref",
        default=argparse.SUPPRESS,
        help=CLI_HELP["arg_ref"],
    )
    base_parser.add_argument(
        "--threads",
        "-t",
        type=int,
        default=argparse.SUPPRESS,
        help=CLI_HELP["arg_threads"],
    )
    base_parser.add_argument(
        "--memory",
        "-m",
        default=argparse.SUPPRESS,
        help=CLI_HELP["arg_memory"],
    )
    base_parser.add_argument(
        "--parent-pid",
        type=int,
        help="Parent Process ID to monitor. If the parent dies, this process will exit.",
    )

    # 2. Main parser
    parser = argparse.ArgumentParser(
        description=CLI_HELP["description"], parents=[base_parser]
    )
    parser.set_defaults(
        debug=settings.get("debug_mode", False),
        quiet=settings.get("quiet_mode", False),
        input=settings.get("input_path"),
        outdir=settings.get("output_directory"),
        genome=None,
        ref=settings.get("reference_fasta"),
        threads=settings.get("cpu_threads"),
        memory=settings.get("memory_limit"),
        parent_pid=None,
    )
    parser.add_argument(
        "--full-help",
        action="store_true",
        help=CLI_HELP["arg_full_help"],
    )
    subparsers = parser.add_subparsers(
        dest="command", required=False, title="subcommands"
    )

    help_parser = subparsers.add_parser("help", help="Show this concise command tree.")
    help_parser.set_defaults(func=lambda args: print_full_help(parser))

    config_parser = subparsers.add_parser(
        "config", help="Show configuration information."
    )

    def run_config(args):
        config_path = get_config_path()
        print(f"Config Directory: {config_path.parent}")
        print(f"Config File:      {config_path}")

        print("\nConfiguration Settings:")
        print(f"{'Variable':<20} {'Value':<30} {'Status'}")
        print("-" * 65)

        for key, (default, _desc) in KNOWN_SETTINGS.items():
            current_val = settings.get(key)
            if current_val is not None:
                status = "[SET]"
                val_str = str(current_val)
            else:
                status = "[DEFAULT/UNSET]"
                val_str = str(default) if default is not None else "None"

            print(f"{key:<20} {val_str:<30} {status}")

        if not config_path.exists():
            print("\nConfig file does not exist yet.")
            try:
                choice = (
                    input("Would you like to bootstrap a default config.toml? [y/N]: ")
                    .strip()
                    .lower()
                )
                if choice == "y":
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    default_config = """# WGS Extract Configuration

# --- Path Configuration ---
# Default input and output paths
# input = "~/my_genome.bam"
# outdir = "~/wgse_output"

# Path to a specific reference genome FASTA
# ref = "~/my_genome.fa"

# Reference library directory (where multiple genomes are stored)
# reflib = "~/wgse_reference"

# Genome library directory, with one subfolder per person/sample.
# genome_library = "~/wgse_genomes"

# --- Analysis Settings ---
# System resources
# threads = "auto"  # Apple Silicon perf cores, WSL balanced, otherwise all cores
# memory = "16G"

# --- External Tools ---
# Runtime for external tools: auto, native, wsl, cygwin, msys2, or pacman
# runtime = "auto"
# runtime_dir = "runtime"
# pacman_bin = "C:/msys64/ucrt64/bin"

# Paths to specific tool executables or directories
# yleaf_path = "/path/to/yleaf"
# haplogrep_path = "/path/to/haplogrep"
# jar_dir = "/path/to/jars"

# --- Variant Processing ---
# Default VEP cache location
# vep_cache = "~/vep_cache"

# Default VCF inputs for trio or batch analysis
# input_vcf = "/path/to/sample.vcf.gz"
# mother_vcf = "/path/to/mother.vcf.gz"
# father_vcf = "/path/to/father.vcf.gz"
"""
                    config_path.write_text(default_config)
                    print(f"\n✅ Created default config at {config_path}")
                    print("You can now edit this file to persist your settings.")
                else:
                    print("\nSkipping bootstrap.")
            except (EOFError, KeyboardInterrupt):
                print("\nBootstrap cancelled.")

    config_parser.set_defaults(func=run_config)

    # 3. Register all subcommands, passing the base_parser as a parent
    for cmd_module in [
        info,
        deps,
        examples,
        bam,
        benchmark,
        extract,
        microarray,
        lineage,
        vcf,
        repair,
        qc,
        ref,
        align,
        realign,
        pet,
        vep,
        analyze,
    ]:
        cmd_module.register(subparsers, base_parser)

    command_names = set(subparsers.choices)
    option_map = {
        option: action
        for action in base_parser._actions
        for option in action.option_strings
    }
    raw_argv = sys.argv[1:]
    command_index = _find_subcommand_index(raw_argv, command_names, option_map)

    args = parser.parse_args()

    if command_index is not None:
        pre_subcommand_args = _extract_shared_options(
            raw_argv[:command_index], option_map
        )
        post_subcommand_args = _extract_shared_options(
            raw_argv[command_index + 1 :], option_map
        )
        post_subcommand_dests = post_subcommand_args
        for dest, value in pre_subcommand_args.items():
            if dest not in post_subcommand_dests:
                setattr(args, dest, value)

        explicit_dests = set(pre_subcommand_args) | set(post_subcommand_args)
    else:
        explicit_dests = set(_extract_shared_options(raw_argv, option_map))

    args._explicit_dests = set(explicit_dests)

    if args.full_help:
        print_full_help(parser)
        sys.exit(0)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.ERROR)

    # Handle signals for clean exit (allows finally blocks to run)
    import signal

    def signal_handler(signum, frame):
        logging.info(f"Received signal {signum}, cleaning up...")
        cleanup_processes()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Parental monitoring
    if args.parent_pid:
        import threading
        import time

        def monitor_parent():
            while True:
                if not _parent_process_is_alive(args.parent_pid):
                    logging.warning(
                        f"Parent process {args.parent_pid} disappeared, exiting..."
                    )
                    cleanup_processes()
                    os._exit(
                        1
                    )  # Use os._exit to bypass any other cleanup and exit immediately
                time.sleep(2)

        monitor_thread = threading.Thread(target=monitor_parent, daemon=True)
        monitor_thread.start()

    if hasattr(args, "func"):
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
