"""The parsers generated from ``wgsextract_cli.api`` Params match the
hand-written ones they replaced: same options, help text and parse results."""

import functools
import json
from typing import Any

import pytest
from cli_snapshot import GOLDEN, normalise_funcs, snapshot

_GOLDEN = normalise_funcs(json.loads(GOLDEN.read_text()))


@functools.cache
def _current() -> dict[str, Any]:
    # Built on first use, inside a test, so the user's config is isolated.
    return normalise_funcs(snapshot())


@pytest.mark.parametrize("command", sorted(_GOLDEN["commands"]))
def test_parser_structure_unchanged(command: str) -> None:
    assert (
        _current()["commands"][command]["parser"]
        == (_GOLDEN["commands"][command]["parser"])
    )


@pytest.mark.parametrize("command", sorted(_GOLDEN["commands"]))
def test_help_text_unchanged(command: str) -> None:
    assert (
        _current()["commands"][command]["help"] == _GOLDEN["commands"][command]["help"]
    )


@pytest.mark.parametrize("argv", sorted(_GOLDEN["namespaces"]))
def test_parsed_namespace_unchanged(argv: str) -> None:
    assert _current()["namespaces"][argv] == _GOLDEN["namespaces"][argv]
