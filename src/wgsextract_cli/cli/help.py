"""Navigable help: the tiered top-level map, 'wgsextract help', typo hints.

Top-level help leads with the core tier (the consumer journey) from
commands/registry.py; 'help --all' lists every runnable command in one line
each, and each command's --help ends with its examples and outputs from
commands/_help_catalog.py. Colour is used only on a terminal and never when
NO_COLOR is set (https://no-color.org), so piped output stays plain.
"""

import argparse
import difflib
import json
import os
import re
import sys
import textwrap
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Any

from wgsextract_cli.commands._help_catalog import COMMAND_HELP, CommandHelp
from wgsextract_cli.commands.registry import (
    ADVANCED,
    COMMAND_TIERS,
    CORE,
    SETUP,
    TIERS,
    command_tier,
)
from wgsextract_cli.core.messages import CLI_HELP

HELP_SCHEMA = "wgsextract.help/1"
TIER_TITLES = {
    CORE: "Start here, in this order",
    SETUP: "Setup",
    ADVANCED: "Advanced",
}
_NAME_WIDTH = 16
INDEX_WIDTH = 100
_TOP_ORDER = {command.name: index for index, command in enumerate(COMMAND_TIERS)}
_TOP_SUMMARY = {command.name: command.summary for command in COMMAND_TIERS}
_FIRST_RUN = (
    "wgsextract info --input sample.cram",
    "wgsextract microarray --input sample.cram --formats 23andme_v5",
)
_TERMS = (
    "BAM/CRAM = aligned reads; FASTQ = raw reads; VCF = a list of variants; "
    "build = reference genome version (hg19, hg38, T2T); haplogroup = the "
    "branch of the human family tree a paternal or maternal line belongs to."
)


def use_color(stream: IO[str] | None = None) -> bool:
    """Colour only for a terminal, and never with NO_COLOR or TERM=dumb."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def _style(text: str, code: str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def _heading(text: str, color: bool) -> str:
    return _style(text, "1", color)


def _name(text: str, color: bool) -> str:
    return _style(text, "36", color)


def plain_help(text: str | None) -> str:
    """Help strings are argparse %-format templates; render '%%' outside argparse."""
    return (text or "").replace("%%", "%")


def first_sentence(text: str) -> str:
    sentences = re.split(
        r"(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?])\s+(?=[A-Z(])", " ".join(text.split())
    )
    return sentences[0]


@dataclass(frozen=True)
class CommandEntry:
    path: str
    parser: argparse.ArgumentParser
    summary: str
    help: CommandHelp | None

    @property
    def tier(self) -> str:
        return command_tier(self.path)

    @property
    def runnable(self) -> bool:
        return "func" in self.parser._defaults


def _subcommands(
    parser: argparse.ArgumentParser,
) -> Iterator[tuple[str, argparse.ArgumentParser, str]]:
    """(name, parser, help) for each subcommand of parser, once per parser."""
    seen: list[argparse.ArgumentParser] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {choice.dest: choice.help for choice in action._choices_actions}
        for name, subparser in action.choices.items():
            if any(subparser is done for done in seen):
                continue  # an alias
            seen.append(subparser)
            yield name, subparser, plain_help(helps.get(name))


def walk_commands(
    parser: argparse.ArgumentParser, prefix: str = ""
) -> Iterator[CommandEntry]:
    """Every command below parser, depth first, with its original summary."""
    for name, subparser, help_text in _subcommands(parser):
        path = f"{prefix} {name}".strip()
        summary = getattr(subparser, "summary", None) or help_text
        yield CommandEntry(path, subparser, summary, COMMAND_HELP.get(path))
        yield from walk_commands(subparser, path)


def _sort_key(entry: CommandEntry) -> tuple[int, int]:
    return (TIERS.index(entry.tier), _TOP_ORDER.get(entry.path.split(" ")[0], 99))


def command_index(
    parser: argparse.ArgumentParser, prefix: str = ""
) -> list[CommandEntry]:
    """Runnable commands under prefix, by tier and then journey order."""
    if prefix:
        parser = resolve_command(parser, prefix.split())
    entries = [e for e in walk_commands(parser, prefix) if e.runnable]
    if prefix and "func" in parser._defaults:
        entries.insert(0, _entry_for(parser, prefix))
    return sorted(entries, key=_sort_key)


def _entry_for(parser: argparse.ArgumentParser, path: str) -> CommandEntry:
    return CommandEntry(
        path, parser, getattr(parser, "summary", "") or "", COMMAND_HELP.get(path)
    )


def apply_help_metadata(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    """Attach each command's summary and examples/outputs to its parser, and
    list a mixed-tier group's core subcommands first, marking the rest
    "(advanced)"."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        action.metavar = "<command>"
        group_tier = command_tier(prefix) if prefix else None
        tiers: dict[str, str] = {}
        for choice in action._choices_actions:
            path = f"{prefix} {choice.dest}".strip()
            subparser = action.choices[choice.dest]
            subparser.summary = (
                _TOP_SUMMARY[path]
                if not prefix
                else first_sentence(plain_help(choice.help))
            )
            subparser.command_help = COMMAND_HELP.get(path)
            tiers[choice.dest] = command_tier(path)
            apply_help_metadata(subparser, path)
        if group_tier is not None and group_tier != ADVANCED:
            for choice in action._choices_actions:
                if tiers[choice.dest] == ADVANCED and choice.help:
                    choice.help = f"(advanced) {choice.help}"
            action._choices_actions.sort(key=lambda c: tiers[c.dest] == ADVANCED)


def command_map(color: bool) -> str:
    """The tiered, journey-ordered command list for top-level help."""
    lines: list[str] = []
    for tier in (CORE, SETUP):
        lines.append(_heading(f"{TIER_TITLES[tier]}:", color))
        for command in COMMAND_TIERS:
            if command.tier == tier and command.name != "help":
                pad = " " * max(1, _NAME_WIDTH - len(command.name))
                lines.append(f"  {_name(command.name, color)}{pad}{command.summary}")
        lines.append("")
    advanced = ", ".join(c.name for c in COMMAND_TIERS if c.tier == ADVANCED)
    lines += _wrap(f"{TIER_TITLES[ADVANCED]}: {advanced}", color, TIER_TITLES[ADVANCED])
    lines += _wrap(
        "More: 'wgsextract help --all' lists every command; 'wgsextract help "
        "vcf snp' (or 'wgsextract vcf snp --help') explains one.",
        color,
        "More",
    )
    lines.append("")
    lines.append(_heading("Example, a 23andMe-style file from a CRAM:", color))
    lines += [f"  {example}" for example in _FIRST_RUN]
    lines.append("")
    lines += _wrap(f"Terms: {_TERMS}", color, "Terms")
    return "\n".join(lines) + "\n"


def _wrap(text: str, color: bool, label: str) -> list[str]:
    lines = textwrap.wrap(text, width=78, subsequent_indent="  ")
    lines[0] = _heading(f"{label}:", color) + lines[0][len(label) + 1 :]
    return lines


def contract_text(command_help: CommandHelp, color: bool) -> str:
    """The examples and outputs sections that end a command's --help."""
    lines = [_heading("examples:", color)]
    lines += [f"  {example}" for example in command_help.examples]
    lines += ["", _heading("outputs (in the output directory unless noted):", color)]
    lines += textwrap.wrap(
        command_help.outputs, width=78, initial_indent="  ", subsequent_indent="  "
    )
    return "\n".join(lines) + "\n"


def resolve_command(
    parser: argparse.ArgumentParser, tokens: Iterable[str]
) -> argparse.ArgumentParser:
    """The parser for a command path such as ['vcf', 'snp'].

    Raises ValueError with a "did you mean" hint for an unknown name."""
    path: list[str] = []
    for token in tokens:
        choices = {name: sub for name, sub, _help in _subcommands(parser)}
        if token not in choices:
            raise ValueError(unknown_command_message(" ".join(path), token, choices))
        parser = choices[token]
        path.append(token)
    return parser


def suggestions(value: str, choices: Iterable[Any]) -> list[str]:
    """Up to three choices close to value, ignoring case."""
    names = {str(choice).lower(): str(choice) for choice in choices}
    if value.lower() in names:
        return [names[value.lower()]]
    close = difflib.get_close_matches(value.lower(), list(names), n=3, cutoff=0.6)
    if not close:
        close = [name for name in names if name.startswith(value.lower())][:3]
    return [names[name] for name in close]


def did_you_mean(value: str, choices: Iterable[Any]) -> str:
    close = suggestions(value, choices)
    if not close:
        return ""
    return "Did you mean " + " or ".join(f"'{name}'" for name in close) + "?"


def unknown_command_message(group: str, value: str, choices: Iterable[Any]) -> str:
    """A usage error for an unknown (sub)command, with a "did you mean" hint
    and where to find the command list."""
    names = [str(choice) for choice in choices]
    kind = f"'{group}' subcommand" if group else "command"
    message = f"unknown {kind} '{value}'."
    hint = did_you_mean(value, names)
    if hint:
        message += f" {hint}"
    elif group:
        message += f" Choose from: {', '.join(names)}."
    see = f"wgsextract help {group}" if group else "wgsextract help --all"
    return f"{message} See '{see}'."


def format_index(entries: list[CommandEntry], color: bool) -> str:
    """'help --all': one line per command, grouped by tier, each summary cut
    to fit INDEX_WIDTH columns ('help <command>' has the rest)."""
    width = max((len(entry.path) for entry in entries), default=0) + 2
    room = max(20, INDEX_WIDTH - 2 - width)
    lines: list[str] = []
    for tier in TIERS:
        tier_entries = [entry for entry in entries if entry.tier == tier]
        if not tier_entries:
            continue
        if lines:
            lines.append("")
        lines.append(_heading(f"{TIER_TITLES[tier]}:", color))
        for entry in tier_entries:
            pad = " " * (width - len(entry.path))
            summary = textwrap.shorten(entry.summary, room, placeholder="...")
            lines.append(f"  {_name(entry.path, color)}{pad}{summary}")
    return "\n".join(lines) + "\n"


def index_json(entries: list[CommandEntry]) -> str:
    """'help --all --json': the command index as a stable JSON document.

    "usage" is argparse's usage line, so the arguments are visible without
    loading each command's --help."""
    commands = []
    for entry in entries:
        usage = " ".join(entry.parser.format_usage().split())
        commands.append(
            {
                "path": entry.path,
                "tier": entry.tier,
                "summary": entry.summary,
                "usage": usage.removeprefix("usage: "),
                "examples": list(entry.help.examples) if entry.help else [],
                "outputs": entry.help.outputs if entry.help else None,
            }
        )
    return json.dumps({"schema": HELP_SCHEMA, "commands": commands}, indent=2) + "\n"


def run_help(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """The 'help' command: the command tree, the index (--all), or one command."""
    topic = list(getattr(args, "topic", None) or [])
    try:
        target = resolve_command(parser, topic)
    except ValueError as error:
        help_parser = resolve_command(parser, ["help"])
        help_parser.error(str(error))
    color = use_color()
    if getattr(args, "json", False):
        sys.stdout.write(index_json(command_index(parser, " ".join(topic))))
    elif getattr(args, "all_commands", False):
        sys.stdout.write(format_index(command_index(parser, " ".join(topic)), color))
    elif topic:
        target.print_help()
    else:
        print_full_help(parser)


def print_full_help(parser: argparse.ArgumentParser) -> None:
    """Print a concise tree of all commands and subcommands."""
    print("\n" + CLI_HELP["description"])
    print("\nGLOBAL OPTIONS:")
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction) and action.dest != "help":
            opts = ", ".join(action.option_strings)
            if opts:
                print(f"  {opts:<25} {plain_help(action.help)}")

    print("\nCOMMAND TREE:")
    _print_tree_recursive(parser, 0)


def _print_tree_recursive(parser: argparse.ArgumentParser, indent: int) -> None:
    for name, subparser, help_text in sorted(_subcommands(parser)):
        if not help_text and subparser.description:
            help_text = subparser.description.split("\n")[0]
        print(f"{'  ' * indent}- {name:<20} {help_text}")
        _print_tree_recursive(subparser, indent + 1)


def render_markdown(parser: argparse.ArgumentParser) -> str:
    """docs/commands.md: the command reference, generated from the tiers and
    the help catalog."""
    out = [
        "# Command reference",
        "",
        "<!-- Generated by `pixi run gen-command-docs` from "
        "src/wgsextract_cli/commands/registry.py (tiers) and _help_catalog.py "
        "(examples, outputs). Don't edit by hand. -->",
        "",
        "Commands are grouped by tier. The core tier is the usual path, in "
        "order: check a file, make microarray files or find haplogroups, "
        "extract reads, (re)align, call and annotate variants. Outputs go to "
        "the output directory (`--outdir`, the configured `outdir` or the "
        "`--genome` folder) unless noted, each with a `.manifest.json`. Run "
        "`wgsextract help <command>` for every option.",
    ]
    entries = command_index(parser)
    for tier in TIERS:
        out += ["", f"## {TIER_TITLES[tier]}", ""]
        out += [
            "| Command | What it does | Writes | Example |",
            "| --- | --- | --- | --- |",
        ]
        for entry in (e for e in entries if e.tier == tier):
            example = entry.help.examples[0] if entry.help else ""
            prose = [entry.summary, entry.help.outputs if entry.help else ""]
            cells = [
                f"`{entry.path}`",
                *(text.replace("<", "&lt;") for text in prose),
                f"`{example}`",
            ]
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(out) + "\n"
