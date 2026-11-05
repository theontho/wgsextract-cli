"""Navigable help: the tiered top-level map, 'wgsextract help', typo hints.

Top-level help leads with the core tier (the consumer journey) from
commands/registry.py; 'help --all' lists every runnable command in one line
each, and each command's --help ends with its examples and outputs from
commands/_help_catalog.py. Colour is used only on a terminal and never when
NO_COLOR is set (https://no-color.org), so piped output stays plain.
"""

import argparse
import difflib
import functools
import json
import os
import re
import sys
import textwrap
import typing
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import IO, Any

from wgsextract_cli.commands._advanced_options import ADVANCED_OPTIONS, COMMON_ADVANCED
from wgsextract_cli.commands._command_needs import COMMAND_NEEDS, CommandNeeds
from wgsextract_cli.commands._help_catalog import COMMAND_HELP, CommandHelp
from wgsextract_cli.commands._help_topics import HELP_TOPICS
from wgsextract_cli.commands.registry import (
    ADVANCED,
    COMMAND_TIERS,
    CORE,
    SETUP,
    TIERS,
    command_tier,
)
from wgsextract_cli.core.config import settings_suspended
from wgsextract_cli.core.messages import CLI_HELP

HELP_SCHEMA = "wgsextract.help/1"
TIER_TITLES = {
    CORE: "Start here, in this order",
    SETUP: "Setup",
    ADVANCED: "Advanced",
}
_WINDOWS = os.name == "nt"
_NAME_WIDTH = 16
INDEX_WIDTH = 100
_TOP_ORDER = {command.name: index for index, command in enumerate(COMMAND_TIERS)}
_TOP_SUMMARY = {command.name: command.summary for command in COMMAND_TIERS}
_FIRST_RUN = (
    "wgsextract info --input sample.cram",
    "wgsextract microarray --input sample.cram --formats 23andme_v5",
)
_TOPICS_HINT = (
    "Topics: references, builds, exit codes and more: 'wgsextract help topics'."
)
_TERMS = (
    "BAM/CRAM = aligned reads; FASTQ = raw reads; VCF = a list of variants; "
    "build = reference genome version (hg19, hg38, T2T); haplogroup = the "
    "branch of the human family tree a paternal or maternal line belongs to."
)


def use_color(stream: IO[str] | None = None) -> bool:
    """Colour only for a terminal, and never with NO_COLOR or TERM=dumb. On
    Windows, only if the console accepts ANSI codes (legacy conhost would
    print them raw)."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
        return not _WINDOWS or _enable_windows_vt(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


@functools.cache
def _enable_windows_vt(fd: int) -> bool:
    """Turn on ENABLE_VIRTUAL_TERMINAL_PROCESSING for the console behind fd;
    False if it isn't a console or refuses the mode."""
    if sys.platform != "win32":
        return False
    import ctypes
    import msvcrt
    from ctypes import wintypes

    enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        handle = msvcrt.get_osfhandle(fd)
    except OSError:
        return False
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    if mode.value & enable_vt:
        return True
    return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))


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
            subparser.command_needs = COMMAND_NEEDS.get(path)
            subparser.advanced_actions = advanced_actions(subparser, path)
            tiers[choice.dest] = command_tier(path)
            apply_help_metadata(subparser, path)
        if group_tier is not None and group_tier != ADVANCED:
            for choice in action._choices_actions:
                if tiers[choice.dest] == ADVANCED and choice.help:
                    choice.help = f"(advanced) {choice.help}"
            action._choices_actions.sort(key=lambda c: tiers[c.dest] == ADVANCED)


def advanced_actions(
    parser: argparse.ArgumentParser, path: str
) -> list[argparse.Action]:
    """The options of the command at path that only --help-all shows."""
    flags = COMMON_ADVANCED | set(ADVANCED_OPTIONS.get(path, ()))
    return [
        action
        for action in parser._actions
        if flags.intersection(action.option_strings)
        and action.help is not argparse.SUPPRESS
    ]


def advanced_note(prog: str, count: int) -> str:
    """The line that ends --help when advanced options are left out."""
    noun = "option" if count == 1 else "options"
    return f"{count} advanced {noun} not shown; '{prog} --help-all' lists them.\n"


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
        "vcf snp' (or 'wgsextract vcf snp --help') explains one; 'wgsextract "
        "help topics' lists concepts such as references and exit codes. For "
        "scripts and AI agents: 'wgsextract help --all --json'.",
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


def contract_text(
    command_help: CommandHelp, color: bool, needs: CommandNeeds | None = None
) -> str:
    """The examples, outputs and needs sections that end a command's --help."""
    lines = [_heading("examples:", color)]
    lines += [f"  {example}" for example in command_help.examples]
    sections = [
        ("outputs (in the output directory unless noted):", command_help.outputs)
    ]
    if needs is not None:
        sections.append(("needs:", f"{needs.tools_text}; {needs.runtime_text}."))
    for heading, text in sections:
        lines += ["", _heading(heading, color)]
        lines += textwrap.wrap(
            text, width=78, initial_indent="  ", subsequent_indent="  "
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


def unknown_option_message(prog: str, option: str, known: Iterable[str]) -> str:
    """A usage error for an unknown option, with a "did you mean" hint."""
    name = option.partition("=")[0]
    message = f"unrecognized option '{name}'."
    hint = did_you_mean(name, known)
    if hint:
        message += f" {hint}"
    return f"{message} See '{prog} --help'."


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


def index_json(
    entries: list[CommandEntry], global_options: list[dict[str, Any]] | None = None
) -> str:
    """'help --all --json': the command index as a stable JSON document; with
    global_options (the whole index), also those and the help topics.

        "usage" is argparse's usage line and "options" each command's own options
        (the global ones are listed once, under "global_options"). Commands with
        a typed Params class (wgsextract_cli.api) also carry its JSON Schema as
        "input_schema", each property naming its flag in "x-cli-flag". "tools"
        are the external tools a run checks for, "optional_tools" those only
        some options use, and "runtime" one of instant, seconds, minutes, hours
        or download, for a 30x genome unless "runtime_text" says otherwise."""
    schemas = input_schemas()
    commands = []
    for entry in entries:
        usage = " ".join(entry.parser.format_usage().split())
        command: dict[str, Any] = {
            "path": entry.path,
            "tier": entry.tier,
            "summary": entry.summary,
            "usage": usage.removeprefix("usage: "),
            "options": [
                option_json(entry.parser, action, advanced)
                for action, advanced in _command_options(entry.parser)
            ],
            "examples": list(entry.help.examples) if entry.help else [],
            "outputs": entry.help.outputs if entry.help else None,
        }
        needs = COMMAND_NEEDS.get(entry.path)
        if needs is not None:
            command["tools"] = list(needs.tools)
            command["optional_tools"] = list(needs.optional)
            command["runtime"] = needs.runtime
            command["runtime_text"] = needs.runtime_text
        if entry.path in schemas:
            command["input_schema"] = schemas[entry.path]
        commands.append(command)
    document: dict[str, Any] = {"schema": HELP_SCHEMA}
    if global_options is not None:
        document["global_options"] = global_options
    document["commands"] = commands
    if global_options is not None:
        document["topics"] = [
            {"name": name, "summary": topic.summary}
            for name, topic in HELP_TOPICS.items()
        ]
    return dump_json(document)


def dump_json(document: dict[str, Any]) -> str:
    """Compact JSON with each item of a top-level list on its own line: few
    tokens for an agent, and still greppable ('grep "path":"vcf snp"')."""

    def compact(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    fields = []
    for key, value in document.items():
        if isinstance(value, list) and value:
            value_text = "[\n" + ",\n".join(compact(item) for item in value) + "\n]"
        else:
            value_text = compact(value)
        fields.append(f"{compact(key)}:{value_text}")
    return "{" + ",\n".join(fields) + "}\n"


def _command_options(
    parser: argparse.ArgumentParser,
) -> Iterator[tuple[argparse.Action, bool]]:
    """(action, advanced) for each documented option of the command itself."""
    advanced = getattr(parser, "advanced_actions", [])
    for action in parser._actions:
        if (
            action.help is argparse.SUPPRESS
            or getattr(action, "is_global", False)
            or isinstance(action, (argparse._HelpAction, argparse._SubParsersAction))
        ):
            continue
        yield action, action in advanced


def global_options_json(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """The global options, accepted before or after any command."""
    return [
        option_json(parser, action, False)
        for action in parser._actions
        if getattr(action, "is_global", False)
    ]


def option_json(
    parser: argparse.ArgumentParser, action: argparse.Action, advanced: bool
) -> dict[str, Any]:
    """One option, compactly: keys that would be empty or false are left out."""
    option: dict[str, Any] = {}
    if action.option_strings:
        option["flags"] = list(action.option_strings)
    else:
        option["positional"] = True
        option["metavar"] = action.metavar or action.dest
    option["dest"] = action.dest
    option["type"] = _value_type(action)
    if action.nargs not in (None, 0):
        option["nargs"] = action.nargs
    if action.choices:
        option["choices"] = [str(choice) for choice in action.choices]
    default = _json_default(parser._defaults.get(action.dest, action.default))
    if default is not None and option["type"] != "flag":
        option["default"] = default
    if action.required:
        option["required"] = True
    if advanced:
        option["advanced"] = True
    option["help"] = first_sentence(_expanded_help(parser, action))
    return option


def _json_default(value: Any) -> Any:
    """A default as JSON, or None for one not worth listing (none, empty)."""
    if value in (None, False, argparse.SUPPRESS) or value in ((), [], ""):
        return None
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return value if isinstance(value, bool | int | float) else str(value)


def _value_type(action: argparse.Action) -> str:
    """flag, integer, number or string; a converter function counts as the
    type it's annotated to return."""
    if action.nargs == 0:
        return "flag"
    converter: Any = action.type
    if converter not in (None, int, float) and callable(converter):
        try:
            converter = typing.get_type_hints(converter).get("return")
        except (NameError, TypeError):
            converter = None
    if converter is int:
        return "integer"
    return "number" if converter is float else "string"


def _expanded_help(parser: argparse.ArgumentParser, action: argparse.Action) -> str:
    try:
        return parser._get_formatter()._expand_help(action)
    except (KeyError, TypeError, ValueError):
        return plain_help(action.help)


@functools.cache
def _typed_commands() -> dict[str, Any]:
    from wgsextract_cli import api
    from wgsextract_cli.api._params import Params

    classes = (getattr(api, name) for name in api.__all__)
    return {
        cls.COMMAND: cls
        for cls in classes
        if isinstance(cls, type) and issubclass(cls, Params) and cls.COMMAND
    }


def input_schemas() -> dict[str, dict[str, Any]]:
    """Command path -> the JSON Schema of its typed Params class, with the
    CLI flag of each property as "x-cli-flag"."""
    from wgsextract_cli.api._params import json_schema, param_fields

    schemas = {}
    for path, cls in _typed_commands().items():
        schema = json_schema(cls)
        schema.pop("$schema", None)
        for field in param_fields(cls):
            flag = max(field.flags, key=len)
            schema["properties"][field.name]["x-cli-flag"] = flag
        schemas[path] = schema
    return schemas


def format_topics(color: bool) -> str:
    """'help topics': the concept pages, one line each."""
    width = max(len(name) for name in HELP_TOPICS) + 2
    lines = [_heading("Help topics ('wgsextract help <topic>'):", color)]
    for name, topic in HELP_TOPICS.items():
        pad = " " * (width - len(name))
        lines.append(f"  {_name(name, color)}{pad}{topic.summary}")
    return "\n".join(lines) + "\n"


def topics_json(names: Iterable[str]) -> str:
    topics = [
        {
            "name": name,
            "summary": HELP_TOPICS[name].summary,
            "body": HELP_TOPICS[name].body,
        }
        for name in names
    ]
    return dump_json({"schema": HELP_SCHEMA, "topics": topics})


def run_help(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """The 'help' command: the command tree, the index (--all), a topic, or
    one command."""
    topic = list(getattr(args, "topic", None) or [])
    as_json = getattr(args, "json", False)
    color = use_color()
    if topic == ["topics"] or (len(topic) == 1 and topic[0] in HELP_TOPICS):
        names = list(HELP_TOPICS) if topic == ["topics"] else topic
        if as_json:
            sys.stdout.write(topics_json(names))
        elif topic == ["topics"]:
            sys.stdout.write(format_topics(color))
        else:
            sys.stdout.write(HELP_TOPICS[topic[0]].body)
        return
    try:
        target = resolve_command(parser, topic)
    except ValueError as error:
        message = str(error)
        if len(topic) == 1:
            hint = did_you_mean(topic[0], ["topics", *HELP_TOPICS, *_top_names(parser)])
            message = f"unknown command or topic '{topic[0]}'. "
            message += f"{hint} " if hint else ""
            message += "See 'wgsextract help --all' or 'wgsextract help topics'."
        resolve_command(parser, ["help"]).error(message)
    if as_json:
        # Defaults from config.toml are this user's private paths and
        # machine settings; agents get the defaults declared in code.
        parser = _parser_without_settings()
        entries = command_index(parser, " ".join(topic))
        globals_ = None if topic else global_options_json(parser)
        sys.stdout.write(index_json(entries, globals_))
    elif getattr(args, "all_commands", False):
        sys.stdout.write(format_index(command_index(parser, " ".join(topic)), color))
        if not topic:
            sys.stdout.write(
                "\n" + "\n".join(_wrap(_TOPICS_HINT, color, "Topics")) + "\n"
            )
    elif topic:
        target.print_help()
    else:
        print_full_help(parser)


def _parser_without_settings() -> argparse.ArgumentParser:
    from .parser import build_parser

    with settings_suspended():
        return build_parser()[0]


def _top_names(parser: argparse.ArgumentParser) -> list[str]:
    return [name for name, _sub, _help in _subcommands(parser)]


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
        "src/wgsextract_cli/commands/registry.py (tiers), _help_catalog.py "
        "(examples, outputs) and _command_needs.py (tools, run time). Don't "
        "edit by hand. -->",
        "",
        "Commands are grouped by tier. The core tier is the usual path, in "
        "order: check a file, make microarray files or find haplogroups, "
        "extract reads, (re)align, call and annotate variants. Outputs go to "
        "the output directory (`--outdir`, the configured `outdir` or the "
        "`--genome` folder) unless noted; a run that registers outputs also "
        "writes a `.manifest.json` beside its main output. Run "
        "`wgsextract help <command>` for its options (`--help-all` adds the "
        "advanced ones) and `wgsextract help --all --json` for every "
        "command's options as JSON. *Needs* lists the external tools a "
        "command checks for (`wgsextract deps check` shows which are "
        "installed) and how long it typically takes.",
    ]
    entries = command_index(parser)
    for tier in TIERS:
        out += ["", f"## {TIER_TITLES[tier]}", ""]
        out += [
            "| Command | What it does | Writes | Needs | Example |",
            "| --- | --- | --- | --- | --- |",
        ]
        for entry in (e for e in entries if e.tier == tier):
            example = entry.help.examples[0] if entry.help else ""
            needs = COMMAND_NEEDS.get(entry.path)
            prose = [
                entry.summary,
                entry.help.outputs if entry.help else "",
                f"{needs.tools_text}; {needs.runtime_text}" if needs else "",
            ]
            cells = [
                f"`{entry.path}`",
                *(text.replace("<", "&lt;") for text in prose),
                f"`{example}`",
            ]
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    out += ["", "## Help topics", "", "`wgsextract help <topic>` explains:", ""]
    out += [f"- `{name}`: {topic.summary}" for name, topic in HELP_TOPICS.items()]
    return "\n".join(out) + "\n"
