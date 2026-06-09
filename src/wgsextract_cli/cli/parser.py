import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from wgsextract_cli.commands.registry import COMMAND_MODULES
from wgsextract_cli.core.config import KNOWN_SETTINGS, get_config_path, settings
from wgsextract_cli.core.messages import CLI_HELP

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

# --- Local Developer Test Data ---
# Path to a local real genome dataset used by real smoke tests.
# real_genome_test_path = "~/genomes/sample"
"""


def build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    base_parser = argparse.ArgumentParser(
        add_help=False, argument_default=argparse.SUPPRESS
    )
    add_shared_arguments(base_parser)

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


def print_full_help(parser: argparse.ArgumentParser) -> None:
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
                print(f"{indent_str}- {name:<20} {help_text}")
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
    parse_argv = raw_argv if raw_argv is not None else None

    subparsers = _subparsers_action(parser)
    command_names = set(subparsers.choices)
    option_map = {
        option: action
        for action in base_parser._actions
        for option in action.option_strings
    }
    effective_argv = raw_argv if raw_argv is not None else _sys_argv()
    command_index = _find_subcommand_index(effective_argv, command_names, option_map)

    args = parser.parse_args(parse_argv)

    if command_index is not None:
        pre_subcommand_args = _extract_shared_options(
            effective_argv[:command_index], option_map
        )
        post_subcommand_args = _extract_shared_options(
            effective_argv[command_index + 1 :], option_map
        )
        for dest, value in pre_subcommand_args.items():
            if dest not in post_subcommand_args:
                setattr(args, dest, value)

        explicit_dests = set(pre_subcommand_args) | set(post_subcommand_args)
    else:
        explicit_dests = set(_extract_shared_options(effective_argv, option_map))

    args._explicit_dests = set(explicit_dests)
    return args, explicit_dests


def _sys_argv() -> list[str]:
    import sys

    return sys.argv[1:]


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("Parser has no subparsers action.")


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
