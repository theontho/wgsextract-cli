"""Command tiers and the help system: the tiered top-level help, 'help --all',
'help <command>', typo hints, and the help contract every command meets."""

import io
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from wgsextract_cli.cli import help as cli_help
from wgsextract_cli.cli import hints, parser
from wgsextract_cli.commands._advanced_options import ADVANCED_OPTIONS, COMMON_ADVANCED
from wgsextract_cli.commands._examples_catalog import EXAMPLES
from wgsextract_cli.commands._help_catalog import COMMAND_HELP
from wgsextract_cli.commands._help_topics import HELP_TOPICS
from wgsextract_cli.commands._next_steps import NEXT_STEPS
from wgsextract_cli.commands.registry import COMMAND_TIERS, CORE
from wgsextract_cli.core.config import KNOWN_SETTINGS
from wgsextract_cli.core.results import CommandResult, FileRecord
from wgsextract_cli.main import main

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[")
JOURNEY = [
    "info",
    "microarray",
    "lineage",
    "extract",
    "realign",
    "align",
    "vcf",
    "vep",
    "report",
]


@pytest.fixture(scope="module")
def root() -> parser.argparse.ArgumentParser:
    return parser.build_parser()[0]


@pytest.fixture(scope="module")
def entries(root) -> list[cli_help.CommandEntry]:
    return cli_help.command_index(root)


def _usage_error(argv: list[str], capsys) -> str:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2
    return capsys.readouterr().err


def test_every_command_has_a_tier_and_the_core_tier_is_the_journey(root):
    top = {name for name, _, _ in cli_help._subcommands(root)}
    assert {command.name for command in COMMAND_TIERS} == top
    core = [command.name for command in COMMAND_TIERS if command.tier == CORE]
    assert core == JOURNEY
    assert all(len(command.summary) <= 60 for command in COMMAND_TIERS)


def test_every_runnable_command_meets_the_help_contract(entries):
    paths = {entry.path for entry in entries}
    missing = [
        entry.path
        for entry in entries
        if not (entry.summary and entry.help and entry.help.examples)
        or not entry.help.outputs
    ]
    assert missing == []
    assert set(COMMAND_HELP) - paths == set(), "help catalog has stale commands"


def test_every_group_and_command_renders_its_help(root):
    """#468: 'wgsextract panel --help' crashed on a bare % in a subcommand's
    summary, which argparse %-formats in its parent's help. --help-all also
    renders the advanced options' help."""
    parsers = [root] + [entry.parser for entry in cli_help.walk_commands(root)]
    for command in parsers:
        for show_advanced in (False, True):
            command.show_advanced = show_advanced
            try:
                text = command.format_help()
            finally:
                command.show_advanced = False
            assert "%%" not in text, command.prog


def test_every_example_parses_to_its_own_command(entries):
    for entry in entries:
        assert entry.help is not None
        for example in entry.help.examples:
            words = shlex.split(example)
            assert words[0] == "wgsextract", example
            args, _ = parser.parse_args(*parser.build_parser(), words[1:])
            assert args._command == entry.path, example


def test_example_genome_examples_name_catalog_entries():
    ids = {example.example_id for example in EXAMPLES}
    tags = {tag for example in EXAMPLES for tag in example.tags}
    assert cli_help.COMMAND_HELP["example-genome download"].examples
    for path in ("example-genome download", "example-genome list"):
        for example in COMMAND_HELP[path].examples:
            words = shlex.split(example)[3:]
            if "--tag" in words:
                assert words[words.index("--tag") + 1] in tags
            elif words and not words[0].startswith("-"):
                assert words[0] in ids


def test_top_level_help_starts_with_the_core_journey(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0

    out = capsys.readouterr().out
    core = out.split("Start here, in this order:", 1)[1].split("\n\n", 1)[0]
    assert [line.split()[0] for line in core.splitlines() if line] == JOURNEY
    assert "Advanced: bam, qc," in out and "wgsextract help --all" in out
    assert max(len(line) for line in cli_help.command_map(False).splitlines()) <= 80
    assert not ANSI.search(out)


def test_help_all_prints_one_line_per_runnable_command(capsys, entries):
    main(["help", "--all"])

    lines = [line for line in capsys.readouterr().out.splitlines() if line[:2] == "  "]
    assert [line.split("  ")[1] for line in lines] == [e.path for e in entries]
    assert all(len(line) <= cli_help.INDEX_WIDTH for line in lines)


def test_help_all_for_a_group_lists_its_core_subcommands_first(capsys):
    main(["help", "--all", "vcf"])

    out = capsys.readouterr().out
    core, advanced = out.split("Advanced:")
    assert "vcf snp " in core and "vcf deepvariant " in advanced


def test_help_all_json_is_a_versioned_command_index(capsys, entries):
    main(["help", "--all", "--json"])

    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "wgsextract.help/1"
    assert [command["path"] for command in doc["commands"]] == [e.path for e in entries]
    snp = next(c for c in doc["commands"] if c["path"] == "vcf snp")
    assert snp["tier"] == CORE and snp["examples"] and snp["outputs"]
    assert snp["usage"].startswith("wgsextract vcf snp ")


def test_help_for_a_command_shows_its_examples_and_outputs(capsys):
    main(["help", "vcf", "snp"])

    out = capsys.readouterr().out
    assert out.startswith("usage: wgsextract vcf snp")
    assert "examples:\n  wgsextract vcf snp --input sample.cram" in out
    assert "outputs (in the output directory unless noted):\n  snps.vcf.gz" in out


@pytest.mark.parametrize(
    ("argv", "hint"),
    [
        (["linage"], "unknown command 'linage'. Did you mean 'lineage'?"),
        (["vcf", "snpp"], "unknown 'vcf' subcommand 'snpp'. Did you mean 'snp'?"),
        (["help", "vcf", "snpp"], "Did you mean 'snp'? See 'wgsextract help vcf'."),
        (["vep", "run", "--assembly", "grch38"], "Did you mean 'GRCh38'?"),
        (
            ["info", "--inptu", "x.bam"],
            "unrecognized option '--inptu'. Did you mean '--input'?",
        ),
        (["info", "--inptu"], "Did you mean '--input'? See 'wgsextract info --help'."),
        (
            ["vcf", "snp", "--inptu=x.bam"],
            "unrecognized option '--inptu'. Did you mean '--input'?",
        ),
        (["microarray", "--formts", "all"], "Did you mean '--formats'"),
        (
            ["help", "refrences"],
            "unknown command or topic 'refrences'. Did you mean 'references'?",
        ),
    ],
)
def test_typos_are_usage_errors_with_a_suggestion(argv, hint, capsys):
    assert hint in _usage_error(argv, capsys)


def test_an_unknown_subcommand_without_a_close_match_lists_the_choices(capsys):
    err = _usage_error(["vcf", "zzz"], capsys)
    assert "Choose from: snp, indel," in err


def test_vcf_help_marks_advanced_subcommands(root):
    text = cli_help.resolve_command(root, ["vcf"]).format_help()
    assert text.index("snp ") < text.index("deepvariant")
    assert re.search(r"deepvariant\s+\(advanced\) ", text)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1


def test_colour_only_on_a_terminal_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(cli_help, "_WINDOWS", False)
    assert cli_help.use_color(_Tty()) and not cli_help.use_color(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not cli_help.use_color(_Tty())


def test_no_colour_when_a_windows_console_refuses_ansi(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(cli_help, "_WINDOWS", True)
    monkeypatch.setattr(cli_help, "_enable_windows_vt", lambda fd: False)
    assert not cli_help.use_color(_Tty())
    monkeypatch.setattr(cli_help, "_enable_windows_vt", lambda fd: True)
    assert cli_help.use_color(_Tty())


def test_piped_help_has_no_colour():
    result = subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "help", "--all"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Start here" in result.stdout and not ANSI.search(result.stdout)


def test_help_index_has_no_settings_from_the_users_config(tmp_path):
    private = "joes-private-dir"
    paths = {
        key: f"/home/{private}/{key}"
        for key in (
            "input_path",
            "output_directory",
            "temp_directory",
            "reference_fasta",
            "log_dir",
            "vep_cache_directory",
            "default_input_vcf",
            "mother_vcf_path",
            "clinvar_vcf_path",
        )
    }
    config = tmp_path / "config.toml"
    config.write_text(
        "".join(f'{key} = "{value}"\n' for key, value in paths.items())
        + "cpu_threads = 7\n",
        encoding="utf-8",
    )
    base = [sys.executable, "-m", "wgsextract_cli.main", "--config", str(config)]
    outputs = {}
    for words in (
        ["help", "--all"],
        ["help", "--all", "--json"],
        ["help", "vcf", "--json"],
    ):
        result = subprocess.run(
            [*base, *words], capture_output=True, text=True, check=True
        )
        outputs[" ".join(words)] = result.stdout
    for words, out in outputs.items():
        assert private not in out, words
    doc = json.loads(outputs["help --all --json"])
    threads = next(o for o in doc["global_options"] if "--threads" in o["flags"])
    assert "default" not in threads


def test_first_sentence_keeps_abbreviations():
    text = "Download maps (e.g. GENCODE) for genes. More detail."
    assert cli_help.first_sentence(text) == "Download maps (e.g. GENCODE) for genes."


def test_command_reference_doc_is_current(root):
    doc = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    assert doc == cli_help.render_markdown(root), "run 'pixi run gen-command-docs'"


def _parse(words: list[str]):
    return parser.parse_args(*parser.build_parser(), words)[0]


def test_default_help_leaves_out_advanced_options_that_help_all_shows(capsys):
    with pytest.raises(SystemExit):
        main(["microarray", "--help"])
    short = capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        main(["microarray", "--help-all"])
    assert exit_info.value.code == 0
    full = capsys.readouterr().out
    assert "--min-gq" not in short and "--help-all" in short and "--min-gq" in full
    assert len(short.splitlines()) < len(full.splitlines())
    assert _parse(["microarray", "--input", "x.cram", "--min-gq", "20"]).min_gq == 20


def test_advanced_options_name_real_options(root):
    found: set[str] = set()
    for path, flags in ADVANCED_OPTIONS.items():
        known = cli_help.resolve_command(root, path.split())._option_string_actions
        assert set(flags) <= set(known), path
    for entry in cli_help.command_index(root):
        found.update(entry.parser._option_string_actions)
    assert set(COMMON_ADVANCED) <= found


def test_help_topics_are_listed_readable_and_their_examples_parse(capsys, root):
    assert not (
        {"topics", *HELP_TOPICS} & {n for n, _, _ in cli_help._subcommands(root)}
    )
    for name, topic in HELP_TOPICS.items():
        assert all(len(line) <= 80 for line in topic.body.splitlines()), name
        for line in topic.body.splitlines():
            if line.startswith("  wgsextract "):
                _parse(shlex.split(line)[1:])

    main(["help", "topics"])
    listing = capsys.readouterr().out
    assert all(name in listing for name in HELP_TOPICS)
    main(["help", "exit-codes"])
    assert capsys.readouterr().out == HELP_TOPICS["exit-codes"].body
    main(["help", "--json", "manifests"])
    (topic,) = json.loads(capsys.readouterr().out)["topics"]
    assert topic["name"] == "manifests" and topic["body"]


def test_coverage_topic_quotes_the_default_depth_thresholds(capsys):
    main(["help", "coverage"])
    text = " ".join(capsys.readouterr().out.split())
    diploid = KNOWN_SETTINGS["qc_min_diploid_depth"][0]
    haploid = KNOWN_SETTINGS["qc_min_haploid_depth"][0]
    assert f"warn below {diploid:g}x on autosomes and {haploid:g}x on chrY" in text


def test_typed_commands_embed_their_input_schema(capsys, root):
    main(["help", "--all", "--json"])
    doc = json.loads(capsys.readouterr().out)
    typed = {c["path"]: c for c in doc["commands"] if "input_schema" in c}
    assert {"microarray", "vcf snp", "lineage y-haplogroup"} <= set(typed)
    kinds = {"integer": "integer", "number": "number", "boolean": "flag"}
    for path, command in typed.items():
        known = cli_help.resolve_command(root, path.split())._option_string_actions
        options = {flag: o for o in command["options"] for flag in o.get("flags", [])}
        positionals = {o["metavar"] for o in command["options"] if "positional" in o}
        for prop in command["input_schema"]["properties"].values():
            if "x-cli-positional" in prop:
                assert prop["x-cli-positional"] in positionals, path
                continue
            assert prop["x-cli-flag"] in known, path
            kind = prop["type"][0] if isinstance(prop["type"], list) else prop["type"]
            if prop["x-cli-flag"] in options and kind in ("string", *kinds):
                # The CLI view and the schema agree, custom converters included.
                expected = kinds.get(kind, "string")
                assert options[prop["x-cli-flag"]]["type"] == expected, prop
    qc_depth = next(
        o for o in typed["microarray"]["options"] if o["dest"] == "qc_min_depth"
    )
    assert qc_depth["type"] == "number"


def _value(option: dict) -> str:
    if "choices" in option:
        return option["choices"][0]
    return {"integer": "1", "number": "1.5"}.get(option["type"], "sample.cram")


def test_an_agent_can_go_from_help_json_to_valid_invocations(capsys):
    """Using only 'help --all --json': find the command that makes a
    23andMe-style file, then build an invocation of every command from its
    required options and arguments, and check that each parses."""
    main(["help", "--all", "--json"])
    out = capsys.readouterr().out
    doc = json.loads(out)
    chip = [
        c
        for c in doc["commands"]
        if c["tier"] == CORE and " " not in c["path"] and "23andMe" in c["summary"]
    ]
    assert [c["path"] for c in chip] == ["microarray"]
    schema = chip[0]["input_schema"]
    flag = schema["properties"]["input"]["x-cli-flag"]
    formats = next(o for o in chip[0]["options"] if o["dest"] == "formats")
    args = _parse(
        ["microarray", flag, "sample.cram", formats["flags"][0], "23andme_v5"]
    )
    assert (args._command, args.input, args.formats) == (
        "microarray",
        "sample.cram",
        "23andme_v5",
    )

    for command in doc["commands"]:
        words = command["path"].split()
        for option in command["options"]:
            if option.get("positional") and option.get("nargs") not in ("*", "?"):
                words.append(_value(option))
            elif option.get("required"):
                words.append(option["flags"][0])
                if option["type"] != "flag":
                    words.append(_value(option))
        assert _parse(words)._command == command["path"], words


def test_help_stays_token_efficient(capsys):
    """Byte budgets, not exact text: the index must stay small enough to read
    whole, and one command's JSON much smaller than the full document."""
    with pytest.raises(SystemExit):
        main(["--help"])
    assert len(capsys.readouterr().out) <= 6_000
    main(["help", "--all"])
    assert len(capsys.readouterr().out) <= 12_000
    main(["help", "--all", "--json"])
    # Grows with each command group moved onto typed Params (input_schema).
    assert len(capsys.readouterr().out) <= 300_000
    main(["help", "microarray", "--json"])
    assert len(capsys.readouterr().out) <= 15_000


def test_next_step_hints_parse_to_commands():
    values = {"source": "--input s.cram", "bam": "s.bam", "vcf": "s.vcf.gz"}
    for path, templates in NEXT_STEPS.items():
        assert path in COMMAND_HELP, path
        for template in templates:
            words = shlex.split(template.format(**values))
            assert _parse(words[1:])._command != path, template


def _finished(command: str, *outputs: tuple[str, str]) -> CommandResult:
    result = CommandResult(command=command)
    result.outputs = [FileRecord(path, kind, "output") for path, kind in outputs]
    return result


def test_next_step_hints_only_on_a_terminal_and_not_for_json_or_quiet(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli_help, "_WINDOWS", False)
    args = parser.argparse.Namespace(input="my genome.cram", genome=None)
    result = _finished("vcf snp", ("/data/snps.vcf.gz", "vcf"))

    tty = _Tty()
    hints.print_next_steps(args, result, tty)
    assert "vcf clinvar --vcf-input /data/snps.vcf.gz" in tty.getvalue()
    assert ANSI.search(tty.getvalue())

    for changes in ({"json": True}, {"quiet": True}):
        tty = _Tty()
        hints.print_next_steps(
            parser.argparse.Namespace(**vars(args), **changes), result, tty
        )
        assert tty.getvalue() == "", changes
    piped = io.StringIO()
    hints.print_next_steps(args, result, piped)
    assert piped.getvalue() == ""
    result.exit_code = 1
    hints.print_next_steps(args, result, tty := _Tty())
    assert tty.getvalue() == ""

    monkeypatch.setattr(cli_help, "_WINDOWS", True)
    monkeypatch.setattr(cli_help, "_enable_windows_vt", lambda fd: False)
    hints.print_next_steps(args, _finished("info"), tty := _Tty())
    assert (
        "--input 'my genome.cram'" in tty.getvalue()
        or '"my genome.cram"' in tty.getvalue()
    )
    assert not ANSI.search(tty.getvalue())


def test_next_step_hints_skip_what_the_run_cannot_fill():
    args = parser.argparse.Namespace(input=None, genome="family/joe")
    assert hints.next_steps("realign", args, []) == []
    assert hints.next_steps("microarray", args, [])[0].endswith("--genome family/joe")


def test_next_step_hints_keep_the_runs_input_reference_and_config():
    args = _parse(
        [
            "--config",
            "p.toml",
            "info",
            "--genome",
            "joe",
            "--input",
            "b.cram",
            "--ref",
            "r",
        ]
    )
    (hint, *_) = hints.next_steps("info", args, [])
    assert _parse(shlex.split(hint)[1:]).genome == "joe"
    for words in ("--genome joe --input b.cram", "--ref r", "--config p.toml"):
        assert words in hint
    # --genome alone fills in --input; the hint shouldn't repeat it.
    args = parser.argparse.Namespace(genome="joe", input="/lib/joe/a.cram")
    assert "--input" not in hints.next_steps("info", args, [])[0]
