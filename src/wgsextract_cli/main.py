#!/usr/bin/env python3

import argparse
import logging
import os
import sys


from .commands import (
    align,
    analyze,
    bam,
    deps,
    extract,
    info,
    lineage,
    microarray,
    pet,
    qc,
    ref,
    repair,
    vcf,
    vep,
)
from .core.config import KNOWN_SETTINGS, get_config_path, reload_settings, settings
from .core.messages import CLI_HELP


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


def main():
    # Re-load config to pick up any environment changes made before calling main (useful for tests)
    reload_settings()

    handler = logging.StreamHandler()
    handler.setFormatter(
        EmojiFormatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    )
    # Use force=True to ensure our handler replaces any existing ones
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # 1. Create a parent parser for shared arguments
    # This allows arguments like --input to be placed AFTER the subcommand
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        "--debug",
        action="store_true",
        default=settings.get("debug_mode", False),
        help=CLI_HELP["arg_debug"],
    )
    base_parser.add_argument(
        "--quiet",
        action="store_true",
        default=settings.get("quiet_mode", False),
        help="Suppress all informational logs. (Env: WGSE_QUIET_MODE=1)",
    )
    base_parser.add_argument(
        "--input",
        "-i",
        default=settings.get("input_path"),
        help=CLI_HELP["arg_input"],
    )
    base_parser.add_argument(
        "--outdir",
        "-o",
        default=settings.get("output_directory"),
        help=CLI_HELP["arg_outdir"],
    )
    base_parser.add_argument(
        "--ref",
        default=settings.get("reference_fasta"),
        help=CLI_HELP["arg_ref"],
    )
    base_parser.add_argument(
        "--threads",
        "-t",
        type=int,
        default=settings.get("cpu_threads"),
        help=CLI_HELP["arg_threads"],
    )
    base_parser.add_argument(
        "--memory",
        "-m",
        default=settings.get("memory_limit"),
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
    parser.add_argument(
        "--full-help",
        action="store_true",
        help=CLI_HELP["arg_full_help"],
    )
    subparsers = parser.add_subparsers(
        dest="command", required=False, title="subcommands"
    )

    # UI Commands
    gui_parser = subparsers.add_parser("gui", help=CLI_HELP["cmd_gui"])
    gui_group = gui_parser.add_mutually_exclusive_group()
    gui_group.add_argument(
        "--web", action="store_true", help="Start the Web-based GUI (NiceGUI)."
    )
    gui_group.add_argument(
        "--desktop",
        action="store_true",
        default=True,
        help="Start the Desktop GUI (CustomTkinter).",
    )

    def run_gui(args):
        if args.web:
            __import__("wgsextract_cli.ui.web_gui", fromlist=["main"]).main()
        else:
            __import__("wgsextract_cli.ui.gui", fromlist=["main"]).main()

    gui_parser.set_defaults(func=run_gui)

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

# --- Analysis Settings ---
# System resources
# threads = 8
# memory = "16G"

# --- External Tools ---
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
        bam,
        extract,
        microarray,
        lineage,
        vcf,
        repair,
        qc,
        ref,
        align,
        pet,
        vep,
        analyze,
    ]:
        cmd_module.register(subparsers, base_parser)

    args = parser.parse_args()

    if args.full_help:
        print_full_help(parser)
        sys.exit(0)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.ERROR)

    # Handle signals for clean exit (allows finally blocks to run)
    import signal

    from .core.utils import cleanup_processes

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
                try:
                    # os.kill(pid, 0) is a standard way to check if a process is alive
                    os.kill(args.parent_pid, 0)
                except OSError:
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

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
