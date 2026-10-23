from tests import e2e_base


def test_optional_tool_needs_its_pixi_environment_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_base, "REPO_ROOT", tmp_path)
    deferred = "pixi run -e freebayes freebayes"
    monkeypatch.setattr(e2e_base, "get_tool_path", lambda tool: deferred)
    # Pixi would install the environment on first use: not installed yet.
    assert not e2e_base.optional_tool_installed("freebayes")

    (tmp_path / ".pixi" / "envs" / "freebayes").mkdir(parents=True)
    assert e2e_base.optional_tool_installed("freebayes")

    monkeypatch.setattr(e2e_base, "get_tool_path", lambda tool: None)
    assert not e2e_base.optional_tool_installed("freebayes")

    monkeypatch.setattr(e2e_base, "get_tool_path", lambda tool: "/usr/bin/freebayes")
    assert e2e_base.optional_tool_installed("freebayes")
