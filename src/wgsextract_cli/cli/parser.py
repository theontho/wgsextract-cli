import argparse
import logging
import sys
import textwrap
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from wgsextract_cli import __version__
from wgsextract_cli.commands.registry import COMMAND_MODULES
from wgsextract_cli.core.config import KNOWN_SETTINGS, get_config_path, settings
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.resource_policy import parse_thread_count

from .help import (
    apply_help_metadata,
    command_map,
    contract_text,
    did_you_mean,
    run_help,
    unknown_command_message,
    use_color,
)
from .help import print_full_help as print_full_help  # re-exported

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
# threads = "auto"  # Apple Silicon perf cores, otherwise all cores
# memory = "2G"  # per thread; "auto" uses 25% of RAM split across threads
# keep_awake = false  # opt-in best-effort sleep inhibition during commands

# --- External Tools ---
# Paths to specific tool executables or directories
# haplogrep_path = "/path/to/haplogrep"
# jar_dir = "/path/to/jars"

# --- Variant Processing ---
# Default VEP cache location
# vep_cache = "~/vep_cache"

# Default VCF inputs for trio or batch analysis
# input_vcf = "/path/to/sample.vcf.gz"
# mother_vcf = "/path/to/mother.vcf.gz"
# father_vcf = "/path/to/father.vcf.gz"

# --- Genotype QC warnings (see docs/genotype-qc.md) ---
# qc_min_diploid_depth = 8.0  # autosomes; 0 disables
# qc_min_haploid_depth = 3.0  # chrY, mtDNA, a man's chrX; 0 disables
# qc_min_autosomal_homozygosity = 0.6
# qc_max_autosomal_homozygosity = 0.88
# qc_max_no_call_rate = 0.1

# --- Confidence labels on rare/clinical variants (docs/variant-confidence.md) ---
# confidence_min_depth = 10.0  # below: low confidence (halved for haploid calls)
# confidence_high_depth = 20.0  # below: moderate
# confidence_min_gq = 20.0  # genotype quality below: low
# confidence_high_gq = 30.0  # below: moderate

# --- Local Developer Test Data ---
# Path to a local real genome dataset used by real smoke tests.
# real_genome_test_path = "~/genomes/sample"
# real_genome_tests_enabled = false

# --- Persistent Run Logs (off by default) ---
# log_dir = "~/wgse_logs"
"""


GLOBAL_OPTIONS_TITLE = "global options"


class WgsArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser that rejects a global option given with different
    values before and after a command (``--input a info --input b``),
    suggests close matches for a mistyped command or choice, and ends a
    command's help with its examples and outputs."""

    def format_help(self) -> str:
        text = super().format_help()
        command_help = getattr(self, "command_help", None)
        if command_help is not None:
            text += "\n" + contract_text(command_help, use_color())
        return text

    def _check_value(self, action: argparse.Action, value: Any) -> None:
        try:
            super()._check_value(action, value)
        except argparse.ArgumentError as error:
            if isinstance(action, argparse._SubParsersAction):
                group = self.prog.partition(" ")[2]
                raise argparse.ArgumentError(
                    None, unknown_command_message(group, value, action.choices)
                ) from None
            hint = did_you_mean(str(value), action.choices or ())
            if not hint:
                raise
            raise argparse.ArgumentError(action, f"{error.message}. {hint}") from None

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[no-untyped-def]
        if _global_values.get() is not None:  # a subcommand of this parse
            return super().parse_known_args(args, namespace)
        token = _global_values.set({})
        try:
            result = super().parse_known_args(args, namespace)
            _reject_conflicting_globals(_global_values.get() or {})
            return result
        finally:
            _global_values.reset(token)


class RootArgumentParser(WgsArgumentParser):
    """The top-level parser: its help is the tiered command map, then the
    global options."""

    def format_help(self) -> str:
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        head = formatter.format_help()
        formatter = self._get_formatter()
        for group in self._action_groups:
            actions = [
                action
                for action in group._group_actions
                if not isinstance(action, argparse._SubParsersAction)
            ]
            if actions:
                formatter.start_section(group.title)
                formatter.add_text(group.description)
                formatter.add_arguments(actions)
                formatter.end_section()
        return f"{head}\n{command_map(use_color())}\n{formatter.format_help()}"


# dest -> parser -> (action, option string, value): the last occurrence of
# each global option at each command level of the current parse.
_GlobalValues = dict[
    str, dict[argparse.ArgumentParser, tuple[argparse.Action, str, Any]]
]
_global_values: ContextVar[_GlobalValues | None] = ContextVar(
    "wgsextract_global_values", default=None
)


def _reject_conflicting_globals(seen: _GlobalValues) -> None:
    """Exit with a usage error if two command levels end up with different
    values for a global option. Within one level the last occurrence wins, as
    usual for argparse, so only each level's final value counts."""
    for levels in seen.values():
        (first_action, first_shown, first_value), *_ = levels.values()
        for parser, (action, shown, value) in levels.items():
            if value != first_value:
                error = argparse.ArgumentError(
                    action,
                    f"conflicting values '{first_shown}' and '{shown}'; give "
                    "a global option once, before or after the command",
                )
                parser.error(str(error))


class _GlobalOption(argparse.Action):
    """Mixin for global options: store as the base action does, and record
    the value per command level for _reject_conflicting_globals."""

    def __call__(  # type: ignore[no-untyped-def]
        self, parser, namespace, values, option_string=None
    ) -> None:
        probe = argparse.Namespace()
        super().__call__(parser, probe, values, option_string)
        value = getattr(probe, self.dest)
        seen = _global_values.get()
        if seen is not None:
            shown = option_string or self.dest
            if self.nargs != 0:
                # --threads auto parses to None.
                shown += f" {'auto' if value is None else value}"
            seen.setdefault(self.dest, {})[parser] = (self, shown, value)
        setattr(namespace, self.dest, value)


class _GlobalStore(_GlobalOption, argparse._StoreAction):
    pass


class _GlobalStoreTrue(_GlobalOption, argparse._StoreTrueAction):
    pass


class _GlobalBoolean(_GlobalOption, argparse.BooleanOptionalAction):
    pass


def build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Return the CLI parser and the parent parser that commands inherit
    global options from.

    Global options are accepted on every parser in the tree, so they work
    before or after the command at any depth (``wgsextract --input x vcf snp``
    and ``wgsextract vcf snp --input x`` are the same). Only the top-level
    help lists them in full; each command's help names them in one line.
    """
    base_parser = argparse.ArgumentParser(
        add_help=False, argument_default=argparse.SUPPRESS
    )
    add_shared_arguments(base_parser)
    shared_parser = argparse.ArgumentParser(
        add_help=False, argument_default=argparse.SUPPRESS
    )
    add_shared_arguments(shared_parser, hidden=True)

    parser = RootArgumentParser(
        prog="wgsextract",
        usage="wgsextract [global options] <command> ...",
        description=CLI_HELP["description"],
        parents=[base_parser],
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
        tmp_dir=settings.get("temp_directory"),
        atomic_output=settings.get("atomic_outputs", False),
        parent_pid=None,
        log_file=None,
        log_dir=settings.get("log_dir"),
        keep_awake=settings.get("keep_awake", False),
        json=False,
    )
    parser.add_argument(
        "--version", action="version", version=f"wgsextract {__version__}"
    )
    parser.add_argument(
        "--full-help",
        action="store_true",
        help=CLI_HELP["arg_full_help"],
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=False,
        title="subcommands",
        metavar="<command>",
        prog="wgsextract",
        parser_class=WgsArgumentParser,
    )

    help_parser = subparsers.add_parser(
        "help",
        help="Show the command tree, list every command (--all) or explain one.",
        description="Without arguments, show the command tree. 'help --all' "
        "lists every command in one line each; 'help vcf snp' shows one "
        "command's help. --json prints the command index as JSON.",
    )
    help_parser.add_argument(
        "topic",
        nargs="*",
        metavar="COMMAND",
        help="A command or command path, e.g. 'vcf' or 'vcf snp'.",
    )
    help_parser.add_argument(
        "--all",
        dest="all_commands",
        action="store_true",
        help="List every command (under COMMAND, if given), one line each.",
    )
    help_parser.set_defaults(func=lambda args: run_help(args, parser))

    config_parser = subparsers.add_parser(
        "config", help="Show configuration information."
    )
    config_parser.set_defaults(func=run_config)

    for cmd_module in COMMAND_MODULES:
        cmd_module.register(subparsers, shared_parser)

    _walk_commands(parser, shared_parser)
    apply_help_metadata(parser)
    return parser, base_parser


def _walk_commands(
    parser: argparse.ArgumentParser,
    shared_parser: argparse.ArgumentParser,
    prefix: str = "",
) -> None:
    """Give every subcommand parser the global options it lacks (command
    groups such as ``vcf`` don't inherit them) and a ``_command`` default
    naming its path ("vcf snp"); a deeper parser's default wins. Aliases keep
    the canonical name."""
    seen: list[argparse.ArgumentParser] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            if any(subparser is done for done in seen):
                continue
            seen.append(subparser)
            path = f"{prefix} {name}".strip()
            if not all(a in subparser._actions for a in shared_parser._actions):
                try:
                    subparser._add_container_actions(shared_parser)
                except argparse.ArgumentError as e:
                    raise RuntimeError(
                        f"'wgsextract {path}' defines an option that clashes "
                        f"with a global option: {e}. Rename the local option."
                    ) from e
            subparser.set_defaults(_command=path)
            _walk_commands(subparser, shared_parser, path)


def add_shared_arguments(
    base_parser: argparse.ArgumentParser, *, hidden: bool = False
) -> None:
    """Add the global options to base_parser, in a "global options" group.

    hidden=True (for subcommands) leaves them out of usage lines and lists
    them in one line in the group description instead.
    """
    group = base_parser.add_argument_group(
        GLOBAL_OPTIONS_TITLE,
        "Accepted before or after the command, e.g. 'wgsextract info --input "
        "x.bam'. Different values before and after the command are an error.",
    )
    actions = {
        "store": _GlobalStore,
        "store_true": _GlobalStoreTrue,
        argparse.BooleanOptionalAction: _GlobalBoolean,
    }

    def add(*flags: str, **kwargs: Any) -> None:
        kwargs["action"] = actions[kwargs.get("action", "store")]
        kwargs.setdefault("default", argparse.SUPPRESS)
        if hidden:
            kwargs["help"] = argparse.SUPPRESS
        group.add_argument(*flags, **kwargs)

    add("--input", "-i", help=CLI_HELP["arg_input"])
    add("--outdir", "-o", help=CLI_HELP["arg_outdir"])
    add(
        "--genome",
        help=(
            "Genome ID: a folder in genome_library, such as joe or "
            "test-1000genomes/<id>. Uses that folder for inputs and outputs."
        ),
    )
    add("--ref", help=CLI_HELP["arg_ref"])
    add(
        "--threads",
        "-t",
        type=_thread_count_arg,
        metavar="N|auto",
        help=CLI_HELP["arg_threads"],
    )
    add("--memory", "-m", help=CLI_HELP["arg_memory"])
    add(
        "--tmp-dir",
        metavar="DIRECTORY",
        help="Use an existing writable directory for sort spills, GATK and realign staging (overrides temp_directory).",
    )
    add(
        "--json",
        action="store_true",
        help="Print the result manifest (wgsextract.result/1) as JSON on stdout "
        "instead of human-readable output; logs stay on stderr.",
    )
    add("--quiet", action="store_true", help="Suppress all informational logs.")
    add("--debug", action="store_true", help=CLI_HELP["arg_debug"])
    add(
        "--log-file",
        metavar="PATH",
        help="Write a DEBUG run log and companion provenance JSON (must be a new file).",
    )
    add(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        help="Best-effort idle/sleep inhibition during this command; --no-keep-awake disables it.",
    )
    add(
        "--atomic-output",
        action=argparse.BooleanOptionalAction,
        help="Opt in to atomic main-output publication; staged tool provenance paths may differ.",
    )
    add(
        "--config",
        dest="config_path",
        metavar="FILE",
        help="Read and save settings in this config.toml instead of the per-user one.",
    )
    # Internal: write the result document to PATH without --json's silencing
    # of human output (analyze reads its steps' results this way).
    add("--result-file", dest="_result_file", metavar="PATH", help=argparse.SUPPRESS)
    add(
        "--parent-pid",
        type=int,
        help="Parent Process ID to monitor. If the parent dies, this process will exit.",
    )
    if hidden:
        # Hyphenated names (--tmp-dir) are left out: help wrapping would split
        # them, and raw-text formatters wouldn't wrap a long line at all.
        flags = [
            "/".join(sorted(action.option_strings, key=len))
            for action in group._group_actions
            if "-" not in max(action.option_strings, key=len)[2:]
        ]
        group.description = textwrap.fill(
            f"{', '.join(flags)} and the other global options are accepted "
            "before or after the command; 'wgsextract --help' lists them all.",
            width=76,
        )


def config_path_from_argv(argv: Sequence[str]) -> str | None:
    """The --config value, read before the parser is built (it reads defaults
    from the config). Conflicting occurrences fail later, in parse_args; the
    last one is used until then."""
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
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        interactive = False
    if not interactive:
        print(
            f"Create {config_path} with your settings, or rerun 'wgsextract config' in a terminal."
        )
        return
    try:
        choice = (
            input("Would you like to bootstrap a default config.toml? [y/N]: ")
            .strip()
            .lower()
        )
        if choice == "y":
            config_path.parent.mkdir(parents=True, exist_ok=True)
            from wgsextract_cli.core.atomic_output import atomic_write

            atomic_write(config_path, DEFAULT_CONFIG.encode("utf-8"))
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
    """Parse argv; also return the destinations the user typed (not defaults).

    base_parser is accepted for compatibility; every parser is consulted.
    """
    arg_list = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(arg_list)

    # Parsed again with every default suppressed, the namespace holds only what
    # the user typed, in any spelling argparse accepts. Global options are
    # accepted at every level (`wgsextract --input X info ...`, `wgsextract info
    # --input X coverage-sample`), so a subparser's default must not hide a
    # value given above it; occurrences that disagree are a usage error.
    with _defaults_suppressed(parser) as subcommand_dests:
        typed = vars(parser.parse_args(arg_list))
    explicit_dests = set(typed) - subcommand_dests
    for dest in explicit_dests:
        setattr(args, dest, typed[dest])

    # Commands read this to tell user-supplied options from config defaults.
    args._explicit_dests = set(explicit_dests)
    return args, explicit_dests


@contextmanager
def _defaults_suppressed(parser: argparse.ArgumentParser) -> Iterator[set[str]]:
    """Suppress every default of parser and its subparsers; yield the
    destinations that name a chosen subcommand."""
    saved_defaults: dict[argparse.Action, Any] = {}
    saved_parser_defaults: list[tuple[argparse.ArgumentParser, dict[str, Any]]] = []
    subcommand_dests: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        if any(current is seen for seen, _defaults in saved_parser_defaults):
            continue  # a subcommand alias
        saved_parser_defaults.append((current, current._defaults))
        current._defaults = {}
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                subcommand_dests.add(action.dest)
                pending.extend(action.choices.values())
            if action not in saved_defaults:
                saved_defaults[action] = action.default
                action.default = argparse.SUPPRESS
    try:
        yield subcommand_dests
    finally:
        for action, default in saved_defaults.items():
            action.default = default
        for current, defaults in saved_parser_defaults:
            current._defaults = defaults
