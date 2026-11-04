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
from wgsextract_cli.cli import parser
from wgsextract_cli.commands._examples_catalog import EXAMPLES
from wgsextract_cli.commands._help_catalog import COMMAND_HELP
from wgsextract_cli.commands.registry import COMMAND_TIERS, CORE
from wgsextract_cli.main import main

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[")
JOURNEY = ["info", "microarray", "lineage", "extract", "realign", "align", "vcf", "vep"]


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


def test_first_sentence_keeps_abbreviations():
    text = "Download maps (e.g. GENCODE) for genes. More detail."
    assert cli_help.first_sentence(text) == "Download maps (e.g. GENCODE) for genes."


def test_command_reference_doc_is_current(root):
    doc = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    assert doc == cli_help.render_markdown(root), "run 'pixi run gen-command-docs'"
