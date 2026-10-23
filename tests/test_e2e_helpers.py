from pathlib import Path

import pytest

from tests import e2e_base


def test_optional_tool_needs_its_pixi_environment_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deferred = "pixi run -e freebayes freebayes"
    monkeypatch.setattr(e2e_base, "get_tool_path", lambda tool: deferred)
    # Pixi would install the environment on first use: not installed yet.
    monkeypatch.setattr(e2e_base, "pixi_env_prefix", lambda env: None)
    assert not e2e_base.optional_tool_installed("freebayes")

    # Installed, wherever Pixi keeps it.
    monkeypatch.setattr(e2e_base, "pixi_env_prefix", lambda env: tmp_path / env)
    assert e2e_base.optional_tool_installed("freebayes")

    monkeypatch.setattr(e2e_base, "get_tool_path", lambda tool: None)
    assert not e2e_base.optional_tool_installed("freebayes")

    monkeypatch.setattr(e2e_base, "get_tool_path", lambda tool: "/usr/bin/freebayes")
    assert e2e_base.optional_tool_installed("freebayes")
