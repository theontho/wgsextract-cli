import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from wgsextract_cli.commands.registry import COMMAND_MODULES
from wgsextract_cli.core.config import KNOWN_SETTINGS, get_config_path, settings
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.resource_policy import parse_thread_count

DEFAULT_CONFIG = """# WGS Extract Configuration

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
# memory = "2G"  # per thread; "auto" uses 25% of RAM split across threads

# --- External Tools ---
# Runtime for external tools: auto, native, windows, wsl, cygwin, msys2, or pacman
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

# --- Local Developer Test Data ---
# Path to a local real genome dataset used by real smoke tests.
# real_genome_test_path = "~/genomes/sample"
# real_genome_tests_enabled = false
"""


def build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    base_parser = argparse.ArgumentParser(
        add_help=False, argument_default=argparse.SUPPRESS
    )
    add_shared_arguments(base_parser)

    parser = argparse.ArgumentParser(
        description=CLI_HELP["description"], parents=[base_parser]
    )
    try:
        default_threads = parse_thread_count(settings.get("cpu_threads"))
    except ValueError as e:
        # Don't block every command (even --threads N) on a bad config value.
        logging.warning(f"Ignoring invalid cpu_threads in {get_config_path()}: {e}")
        default_threads = None
    parser.set_defaults(
        debug=settings.get("debug_mode", False),
        quiet=settings.get("quiet_mode", False),
        input=settings.get("input_path"),
        outdir=settings.get("output_directory"),
        genome=None,
        ref=settings.get("reference_fasta"),
        threads=default_threads,
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
    config_parser.set_defaults(func=run_config)

    for cmd_module in COMMAND_MODULES:
        cmd_module.register(subparsers, base_parser)

    return parser, base_parser


def add_shared_arguments(base_parser: argparse.ArgumentParser) -> None:
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
        type=_thread_count_arg,
        metavar="N|auto",
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
        "--config",
        dest="config_path",
        metavar="FILE",
        default=argparse.SUPPRESS,
        help="Read and save settings in this config.toml instead of the per-user one.",
    )
    base_parser.add_argument(
        "--parent-pid",
        type=int,
        help="Parent Process ID to monitor. If the parent dies, this process will exit.",
    )


def config_path_from_argv(argv: Sequence[str]) -> str | None:
    """The --config value, read before the parser is built (it reads defaults
    from the config); the last occurrence wins, as for other shared options."""
    found = None
    tokens = list(argv)
    for index, token in enumerate(tokens):
        if token == "--":
            break
        if token == "--config" and index + 1 < len(tokens):
            found = tokens[index + 1]
        elif token.startswith("--config="):
            found = token.split("=", 1)[1]
    return found


def _thread_count_arg(value: str) -> int | None:
    try:
        return parse_thread_count(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _plain_help(text: str | None) -> str:
    """Help strings are argparse %-format templates; render '%%' outside argparse."""
    return (text or "").replace("%%", "%")


def print_full_help(parser: argparse.ArgumentParser) -> None:
    """Print a concise tree of all commands and subcommands."""
    print("\n" + CLI_HELP["description"])
    print("\nGLOBAL OPTIONS:")
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction) and action.dest != "help":
            opts = ", ".join(action.option_strings)
            if opts:
                print(f"  {opts:<25} {_plain_help(action.help)}")

    print("\nCOMMAND TREE:")
    _print_tree_recursive(parser, 0)


def _print_tree_recursive(parser: argparse.ArgumentParser, indent: int) -> None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            name_to_help = {
                choice_action.dest: choice_action.help
                for choice_action in action._choices_actions
            }

            for name in sorted(action.choices.keys()):
                subparser = action.choices[name]
                help_text = name_to_help.get(name, "")
                if not help_text and subparser.description:
                    help_text = subparser.description.split("\n")[0]

                indent_str = "  " * indent
                print(f"{indent_str}- {name:<20} {_plain_help(help_text)}")
                _print_tree_recursive(subparser, indent + 1)


def run_config(args: argparse.Namespace) -> None:
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
            val_str = _display_config_value(key, current_val)
        else:
            status = "[DEFAULT/UNSET]"
            val_str = _display_config_value(key, default)

        print(f"{key:<20} {val_str:<30} {status}")

    if not config_path.exists():
        bootstrap_default_config(config_path)


def _display_config_value(key: str, value: object) -> str:
    if value is None:
        return "None"
    sensitive_markers = ("token", "secret", "credential", "password", "key")
    if any(marker in key.lower() for marker in sensitive_markers):
        return "<redacted>"
    return str(value)


def bootstrap_default_config(config_path: Path) -> None:
    print("\nConfig file does not exist yet.")
    try:
        choice = (
            input("Would you like to bootstrap a default config.toml? [y/N]: ")
            .strip()
            .lower()
        )
        if choice == "y":
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(DEFAULT_CONFIG)
            print(f"\n✅ Created default config at {config_path}")
            print("You can now edit this file to persist your settings.")
        else:
            print("\nSkipping bootstrap.")
    except (EOFError, KeyboardInterrupt):
        print("\nBootstrap cancelled.")


def parse_args(
    parser: argparse.ArgumentParser,
    base_parser: argparse.ArgumentParser,
    argv: Sequence[str] | None = None,
) -> tuple[argparse.Namespace, set[str]]:
    raw_argv = list(argv) if argv is not None else None
    option_map = {
        option: action
        for action in base_parser._actions
        for option in action.option_strings
    }
    effective_argv = raw_argv if raw_argv is not None else sys.argv[1:]

    args = parser.parse_args(raw_argv)

    # Shared options are accepted at every level (`wgsextract --input X info ...`,
    # `wgsextract info --input X coverage-sample`), but argparse keeps only what
    # the leaf subparser saw. Re-apply them from the whole command line; a later
    # occurrence wins, so options closer to the leaf command take precedence.
    explicit_options = _extract_shared_options(effective_argv, option_map)
    for dest, value in explicit_options.items():
        setattr(args, dest, value)
    explicit_dests = set(explicit_options)

    # Commands read this to tell user-supplied options from config defaults.
    args._explicit_dests = set(explicit_dests)
    return args, explicit_dests


def _extract_shared_options(
    argv: list[str],
    option_map: dict[str, argparse.Action],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
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

        if has_inline_value:
            raw_value = inline_value
        elif i + 1 < len(argv):
            raw_value = argv[i + 1]
        else:
            i += 1
            continue
        values[action.dest] = (
            action.type(raw_value) if callable(action.type) else raw_value
        )
        i += 1 if has_inline_value else 2

    return values
