"""The per-command tools and runtime classes that help publishes stay true to the code."""

import json

import pytest

from tests.dependency_scan import scan, tool_vocabulary
from wgsextract_cli.cli import help as cli_help
from wgsextract_cli.cli import parser
from wgsextract_cli.commands._command_needs import (
    COMMAND_NEEDS,
    HOURS,
    RUNTIME_LIMITS,
    RUNTIMES,
)
from wgsextract_cli.core.warnings import EXPECTED_TIME
from wgsextract_cli.main import main


@pytest.fixture(scope="module")
def entries() -> list[cli_help.CommandEntry]:
    return cli_help.command_index(parser.build_parser()[0])


@pytest.fixture(scope="module")
def scans(entries):
    return {entry.path: scan(entry.parser._defaults["func"]) for entry in entries}


def test_every_runnable_command_declares_its_needs(entries):
    paths = {entry.path for entry in entries}
    assert sorted(paths - set(COMMAND_NEEDS)) == [], "commands without needs"
    assert sorted(set(COMMAND_NEEDS) - paths) == [], "needs for unknown commands"
    vocab = set(tool_vocabulary().values())
    for path, needs in COMMAND_NEEDS.items():
        assert needs.runtime in RUNTIMES, path
        assert set(needs.tools) <= vocab, path
        assert set(needs.optional) <= vocab, path
        assert not set(needs.tools) & set(needs.optional), path


def test_declared_tools_are_those_the_command_checks(scans):
    """Every tool a handler checks for is declared, and nothing else.

    Tools only probed for (used when installed) may be declared optional.
    """
    wrong = {}
    for path, found in scans.items():
        needs = COMMAND_NEEDS[path]
        declared = set(needs.tools) | set(needs.optional)
        if (set(needs.tools) | (declared & found.checked)) != found.checked or not (
            declared <= found.checked | found.probed
        ):
            wrong[path] = {
                "declared": sorted(needs.tools),
                "optional": sorted(needs.optional),
                "checked": sorted(found.checked),
                "probed": sorted(found.probed),
            }
    assert wrong == {}


def test_runtime_classes_agree_with_the_gui_time_estimates(scans):
    """A command that warns with a GUI time estimate is in that estimate's class."""
    mismatched = {}
    for path, found in scans.items():
        needs = COMMAND_NEEDS[path]
        for key in found.warning_keys & set(EXPECTED_TIME):
            seconds = EXPECTED_TIME[key]
            expected = next(
                (cls for cls, limit in RUNTIME_LIMITS.items() if seconds < limit),
                HOURS,
            )
            if needs.runtime != expected:
                mismatched[path] = (key, seconds, needs.runtime)
    assert mismatched == {}


def test_help_publishes_the_needs_in_text_and_json(capsys):
    needs = COMMAND_NEEDS["vcf sv"]
    with pytest.raises(SystemExit):
        main(["vcf", "sv", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    for tool in (*needs.tools, *needs.optional, needs.runtime):
        assert tool in text

    main(["help", "--all", "--json"])
    commands = json.loads(capsys.readouterr().out)["commands"]
    for command in commands:
        declared = COMMAND_NEEDS[command["path"]]
        assert command["tools"] == list(declared.tools)
        assert command["optional_tools"] == list(declared.optional)
        assert command["runtime"] == declared.runtime
