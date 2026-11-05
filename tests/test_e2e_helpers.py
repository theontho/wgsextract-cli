import sys

from tests import e2e_base
from tests.fake_tools import python_tool
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.pixi_tools import PIXI_TOOL_ENVS


def test_optional_tool_needs_its_pixi_environment_installed(fake_pixi, monkeypatch):
    env = PIXI_TOOL_ENVS["freebayes"]
    monkeypatch.setattr(sys, "prefix", str(fake_pixi.env("default")))
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    assert not e2e_base.optional_tool_installed("freebayes")  # nowhere

    # Pixi would install the environment on first use: not installed yet.
    prefix = fake_pixi.env(env, locked=("freebayes",), installed=False)
    python_tool(prefix / "bin", "freebayes", "")
    clear_tool_caches()
    assert not e2e_base.optional_tool_installed("freebayes")

    fake_pixi.install(env)
    clear_tool_caches()
    assert e2e_base.optional_tool_installed("freebayes")


def test_optional_tool_on_the_host_is_installed(fake_pixi):
    python_tool(fake_pixi.host_bin, "freebayes", "")
    assert e2e_base.optional_tool_installed("freebayes")
