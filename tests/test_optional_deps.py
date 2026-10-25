import json
import os
import shlex
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core.dependency_checks import (  # noqa: E402
    check_all_dependencies,
    verify_dependencies,
)
from wgsextract_cli.core.runtime import source_checkout_root
from wgsextract_cli.core.utils import WGSExtractError  # noqa: E402


@pytest.fixture(autouse=True)
def _outside_a_pixi_environment(monkeypatch):
    """These tests simulate the Pixi environment probe; under `pixi run` the CLI
    would look in its own environment first instead."""
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)


MANIFEST = shlex.quote(str(source_checkout_root() / "pixi.toml"))


class TestOptionalDependencies(unittest.TestCase):
    """Tests the new optional dependency handling."""

    @patch("wgsextract_cli.core.dependency_checks.check_dependencies")
    @patch("wgsextract_cli.core.dependencies.get_jar_dir", return_value="/tmp")
    @patch("sys.exit")
    @patch("logging.error")
    @patch("logging.info")
    def test_verify_optional_missing(
        self, mock_info, mock_error, mock_exit, mock_jar, mock_tool_path
    ):
        # Simulate 'minimap2' (optional) is missing
        mock_tool_path.return_value = ["minimap2"]

        with self.assertRaises(WGSExtractError):
            verify_dependencies(["minimap2"])

        # Check that it called logging.error with the optional message
        mock_error.assert_any_call(
            "Required optional tool(s) missing for this feature:"
        )
        mock_error.assert_any_call(" - minimap2")

        # Check for generic installation advice
        args, _ = mock_info.call_args
        self.assertIn("package manager", args[0])
        self.assertIn("brew, apt, conda", args[0])

        mock_exit.assert_not_called()

    @patch("wgsextract_cli.core.dependency_checks.check_dependencies")
    @patch("wgsextract_cli.core.dependencies.get_jar_dir", return_value="/tmp")
    @patch("sys.exit")
    @patch("logging.error")
    def test_verify_mandatory_missing(
        self, mock_error, mock_exit, mock_jar, mock_tool_path
    ):
        # Simulate 'samtools' (mandatory) is missing
        mock_tool_path.return_value = ["samtools"]

        with self.assertRaises(WGSExtractError):
            verify_dependencies(["samtools"])

        # Check that it called logging.error with the fatal message
        mock_error.assert_any_call("Missing required core tools or JAR files.")
        mock_error.assert_any_call(" - samtools")
        mock_exit.assert_not_called()

    @patch("sys.platform", "win32")
    @patch(
        "wgsextract_cli.core.dependency_checks.check_dependencies",
        return_value=["samtools"],
    )
    @patch("wgsextract_cli.core.dependencies.get_jar_dir", return_value="/tmp")
    def test_verify_mandatory_missing_fails_on_windows_too(self, mock_jar, mock_check):
        # Windows runs its tools from Pixi like everywhere else: no runtime to
        # fall back to, so a missing core tool stops the command.
        with self.assertRaisesRegex(WGSExtractError, "samtools"):
            verify_dependencies(["samtools"])

    def test_python_runtime_is_attributed_to_pixi_when_running_inside_pixi_env(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prefix = Path(tempdir) / "envs" / "default"
            python = prefix / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")

            with (
                patch.dict(
                    os.environ,
                    {
                        "CONDA_PREFIX": str(prefix),
                        "PIXI_PROJECT_ROOT": str(Path(tempdir)),
                    },
                    clear=True,
                ),
                patch(
                    "wgsextract_cli.core.dependency_checks.sys.executable", str(python)
                ),
            ):
                results = check_all_dependencies(mandatory=[], optional=[])

        self.assertEqual(results["mandatory"][0]["runtime"], "pixi")

    def test_get_tool_path_can_return_pixi_fallback(self):
        from wgsextract_cli.core.dependencies import get_tool_path

        completed = MagicMock(returncode=0, stdout="direct-name:samtools\n")
        with (
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=lambda tool: (
                    "/usr/local/bin/pixi" if tool == "pixi" else None
                ),
            ),
            patch(
                "wgsextract_cli.core.pixi_tools.subprocess.run",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                get_tool_path("samtools"),
                f"/usr/local/bin/pixi run --manifest-path {MANIFEST} -e default samtools",
            )

    def test_get_tool_path_can_return_uppercase_yleaf_pixi_fallback(self):
        from wgsextract_cli.core.dependencies import get_tool_path

        completed = MagicMock(returncode=0, stdout="direct-name:Yleaf\n")
        with (
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=lambda tool: (
                    "/usr/local/bin/pixi" if tool == "pixi" else None
                ),
            ),
            patch(
                "wgsextract_cli.core.pixi_tools.subprocess.run",
                return_value=completed,
            ) as mock_run,
        ):
            self.assertEqual(
                get_tool_path("yleaf"),
                f"/usr/local/bin/pixi run --manifest-path {MANIFEST} -e default Yleaf",
            )

        self.assertEqual(mock_run.call_args.args[0][-2:], ["yleaf", "Yleaf"])

    @patch("wgsextract_cli.core.dependencies.Path.exists", return_value=True)
    def test_native_windows_tool_path_is_not_shlex_split(self, mock_exists):
        from wgsextract_cli.core.tool_commands import _tool_command_parts

        tool_path = r"C:\Windows\system32\tar.EXE"

        self.assertEqual(_tool_command_parts(tool_path), [tool_path])

    @unittest.skipUnless(sys.platform == "win32", "Windows command-line parsing")
    def test_windows_pixi_command_parts_preserve_backslashes(self):
        from wgsextract_cli.core.tool_commands import _tool_command_parts

        command = (
            r"C:\Users\test\.pixi\bin\pixi.EXE run -e default perl "
            r"C:\repo\.pixi\envs\default\opt\fastqc-0.12.1\fastqc"
        )

        self.assertEqual(
            _tool_command_parts(command),
            [
                r"C:\Users\test\.pixi\bin\pixi.EXE",
                "run",
                "-e",
                "default",
                "perl",
                r"C:\repo\.pixi\envs\default\opt\fastqc-0.12.1\fastqc",
            ],
        )

    def test_shared_required_dependency_policy_excludes_optional_gui_tools(self):
        from wgsextract_cli.core.dependencies import (
            MANDATORY_TOOLS,
            OPTIONAL_TOOLS,
            required_dependency_tools,
        )

        self.assertEqual(required_dependency_tools(), MANDATORY_TOOLS)
        self.assertEqual(
            required_dependency_tools(include_python=False),
            [tool for tool in MANDATORY_TOOLS if tool != "python3"],
        )
        self.assertIn("fastqc", OPTIONAL_TOOLS)
        self.assertNotIn("fastqc", required_dependency_tools(include_python=False))
        self.assertNotIn("fastp", required_dependency_tools(include_python=False))
        self.assertNotIn("curl", required_dependency_tools(include_python=False))

    @patch("wgsextract_cli.core.tool_catalog.MANDATORY_TOOLS", ["samtools"])
    @patch("wgsextract_cli.core.tool_catalog.OPTIONAL_TOOLS", ["minimap2"])
    @patch("wgsextract_cli.core.dependency_checks.get_tool_path")
    @patch("wgsextract_cli.core.dependency_checks.get_tool_version", return_value="1.0")
    def test_deps_check_output(self, mock_version, mock_tool_path):
        from wgsextract_cli.commands.deps import run

        # samtools present, minimap2 missing
        mock_tool_path.side_effect = lambda x, _mode=None: (
            "/usr/bin/" + x if x == "samtools" else None
        )

        with patch("sys.stdout", new=StringIO()) as fake_out:
            args = MagicMock()
            args.tool = None
            run(args)
            output = fake_out.getvalue()

            self.assertIn("Python Runtime", output)
            self.assertIn("✅ samtools", output)
            self.assertIn("[native]", output)
            self.assertIn("⚠️  minimap2", output)
            self.assertIn("Mandatory Tools:", output)
            self.assertIn("Optional Tools:", output)

    @patch("wgsextract_cli.core.tool_catalog.MANDATORY_TOOLS", ["samtools"])
    @patch("wgsextract_cli.core.tool_catalog.OPTIONAL_TOOLS", ["sniffles"])
    @patch("wgsextract_cli.core.dependency_checks.get_tool_path")
    @patch(
        "wgsextract_cli.core.dependency_checks.get_tool_version", return_value="2.7.0"
    )
    def test_deps_check_output_marks_pixi_alt_env_tools(
        self, mock_version, mock_tool_path
    ):
        from wgsextract_cli.commands.deps import run

        mock_tool_path.side_effect = lambda tool, _mode=None: {
            "samtools": "/usr/bin/samtools",
            "sniffles": "/usr/local/bin/pixi run -e pacbio sniffles",
        }.get(tool)

        with patch("sys.stdout", new=StringIO()) as fake_out:
            args = MagicMock()
            args.tool = None
            run(args)
            output = fake_out.getvalue()

        self.assertIn("🔵 sniffles", output)
        self.assertIn("[pixi alt env: pacbio]", output)

    def test_status_text_alt_env_ascii_fallback(self):
        from wgsextract_cli.commands._deps_status import _status_text

        with patch(
            "wgsextract_cli.commands._deps_status._stdout_can_encode",
            return_value=False,
        ):
            self.assertEqual(_status_text("/tool", alt_env=True), "ALT")


if __name__ == "__main__":
    unittest.main()


def test_uninstalled_optional_env_resolves_from_lockfile_without_installing():
    from wgsextract_cli.core import pixi_tools

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1:])
        if cmd[1] == "run":
            # Probes must never install or update an environment.
            assert "--no-install" in cmd
            return MagicMock(returncode=1, stdout="", stderr="env not installed")
        if cmd[1] == "info":
            info = '{"environments_info": [{"name": "gatk", "prefix": "/nonexistent"}]}'
            return MagicMock(returncode=0, stdout=info, stderr="")
        assert cmd[1] == "list" and "--no-install" in cmd
        return MagicMock(returncode=0, stdout='[{"name": "gatk4"}]', stderr="")

    with (
        patch.object(pixi_tools.shutil, "which", return_value="/usr/bin/pixi"),
        patch.object(pixi_tools.subprocess, "run", side_effect=fake_run),
    ):
        command = pixi_tools._host_pixi_tool_command("gatk")

    assert [c[0] for c in calls] == ["run", "list", "info"]
    assert command is not None and command.endswith("-e gatk gatk")
    assert not any(c[0] == "install" for c in calls)


def test_installed_env_without_the_tool_is_unavailable(tmp_path):
    """An installed environment whose probe fails lacks the tool; `pixi run` of
    it would only fail with 'command not found'."""
    from wgsextract_cli.core import pixi_tools

    def fake_run(cmd, **kwargs):
        if cmd[1] == "run":
            return MagicMock(returncode=1, stdout="", stderr="")
        if cmd[1] == "info":
            info = {"environments_info": [{"name": "gatk", "prefix": str(tmp_path)}]}
            return MagicMock(returncode=0, stdout=json.dumps(info), stderr="")
        return MagicMock(returncode=0, stdout='[{"name": "gatk4"}]', stderr="")

    with (
        patch.object(pixi_tools.shutil, "which", return_value="/usr/bin/pixi"),
        patch.object(pixi_tools.subprocess, "run", side_effect=fake_run),
    ):
        assert pixi_tools._host_pixi_tool_command("gatk") is None


def test_install_first_env_is_not_downloaded_on_first_use():
    from wgsextract_cli.core import pixi_tools

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1])
        if cmd[1] == "run":
            return MagicMock(returncode=1, stdout="", stderr="env not installed")
        return MagicMock(returncode=0, stdout='[{"name": "cyrius"}]', stderr="")

    with (
        patch.object(pixi_tools.shutil, "which", return_value="/usr/bin/pixi"),
        patch.object(pixi_tools.subprocess, "run", side_effect=fake_run),
    ):
        # Locked for this platform, but Cyrius's license needs an explicit opt-in.
        assert pixi_tools._host_pixi_tool_command("cyrius") is None
    assert calls == ["run"]


def test_tool_missing_from_platform_lock_is_unavailable():
    from wgsextract_cli.core import pixi_tools

    def fake_run(cmd, **kwargs):
        if cmd[1] == "run":
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout='[{"name": "sniffles"}]', stderr="")

    with (
        patch.object(pixi_tools.shutil, "which", return_value="/usr/bin/pixi"),
        patch.object(pixi_tools.subprocess, "run", side_effect=fake_run),
    ):
        assert pixi_tools._host_pixi_tool_command("pbmm2") is None


def test_resolve_pixi_command_prefers_running_pixi(tmp_path, monkeypatch):
    from wgsextract_cli.core import pixi_tools

    running = tmp_path / "pixi"
    running.write_text("")
    monkeypatch.setenv("PIXI_EXE", str(running))
    with patch.object(
        pixi_tools.shutil, "which", return_value="/opt/homebrew/bin/pixi"
    ):
        assert pixi_tools._resolve_pixi_command() == str(running)


def _fake_pixi(prefix, tool_calls):
    import json

    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["info", "--json"]:
            assert cmd[3:] == ["--manifest-path", "/app/pixi.toml"]
            info = {"environments_info": [{"name": "pacbio", "prefix": str(prefix)}]}
            return MagicMock(returncode=0, stdout=json.dumps(info), stderr="")
        tool_calls.append(cmd)
        return MagicMock(returncode=0, stdout="sniffles 2.6.3", stderr="")

    return fake_run


COMMAND = "/usr/bin/pixi run --manifest-path /app/pixi.toml -e pacbio sniffles"


def test_version_probe_never_runs_a_tool_from_a_missing_env(tmp_path):
    """`pixi run --no-install` runs the tool without the env when it's missing
    (and `exit 0` succeeds), so installation is read from the env's prefix."""
    from wgsextract_cli.core import dependency_checks

    tool_calls: list[list[str]] = []
    prefix = tmp_path / "envs" / "pacbio"  # never installed
    with (
        patch.object(dependency_checks, "get_tool_path", return_value=COMMAND),
        patch.object(
            dependency_checks.subprocess,
            "run",
            side_effect=_fake_pixi(prefix, tool_calls),
        ),
    ):
        version = dependency_checks.get_tool_version("sniffles")

    assert tool_calls == []
    assert version == "Available (Pixi env 'pacbio', downloaded on first use)"


def test_version_probe_runs_an_installed_env_without_installing(tmp_path):
    from wgsextract_cli.core import dependency_checks

    tool_calls: list[list[str]] = []
    prefix = tmp_path / "envs" / "pacbio"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "conda-meta" / "pixi").write_text("{}")
    with (
        patch.object(dependency_checks, "get_tool_path", return_value=COMMAND),
        patch.object(
            dependency_checks.subprocess,
            "run",
            side_effect=_fake_pixi(prefix, tool_calls),
        ),
    ):
        version = dependency_checks.get_tool_version("sniffles")

    assert tool_calls and all("--no-install" in cmd for cmd in tool_calls)
    assert version and "2.6.3" in version


@pytest.mark.parametrize("returncode", [-11, 139])
def test_version_probe_reports_a_crashing_tool(returncode):
    from wgsextract_cli.core import dependency_checks

    with (
        patch.object(
            dependency_checks, "get_tool_path", return_value="/env/bin/sambamba"
        ),
        patch.object(
            dependency_checks.subprocess,
            "run",
            return_value=MagicMock(returncode=returncode, stdout="", stderr=""),
        ),
    ):
        assert dependency_checks.get_tool_version("sambamba") == (
            "Error: sambamba crashed (SIGSEGV)"
        )


def test_uninstalled_windows_script_tool_runs_through_its_interpreter(tmp_path):
    """On Windows `gatk` is an extensionless Python script, so the first-use
    command can't name it; it runs it from the new env with its interpreter."""
    from wgsextract_cli.core import pixi_tools
    from wgsextract_cli.core.tool_commands import _tool_command_parts

    def fake_run(cmd, **kwargs):
        if cmd[1] == "run":
            return MagicMock(returncode=1, stdout="", stderr="env not installed")
        return MagicMock(returncode=0, stdout='[{"name": "gatk4"}]', stderr="")

    with (
        patch.object(pixi_tools.shutil, "which", return_value="/usr/bin/pixi"),
        patch.object(pixi_tools.subprocess, "run", side_effect=fake_run),
        patch.object(pixi_tools.runtime, "is_windows_host", return_value=True),
    ):
        command = pixi_tools._host_pixi_tool_command("gatk")
        assert command is not None
        argv = _tool_command_parts(command)
    stub_at = argv.index("-c") + 1
    assert argv[stub_at - 2 : stub_at - 1] == ["python"]
    assert argv[stub_at + 1 :] == ["python", "gatk"]

    # The stub finds the script in the env prefix and runs it with the launcher.
    (tmp_path / "share" / "gatk4-4.6.2.0-1").mkdir(parents=True)
    (tmp_path / "share" / "gatk4-4.6.2.0-1" / "gatk").write_text("")
    calls = []
    with (
        patch.object(sys, "prefix", str(tmp_path)),
        patch.object(sys, "argv", ["-c", "python", "gatk", "HaplotypeCaller"]),
        patch("subprocess.call", side_effect=lambda cmd: calls.append(cmd) or 0),
        pytest.raises(SystemExit) as exit_info,
    ):
        exec(argv[stub_at], {})
    assert exit_info.value.code == 0
    real_script = tmp_path / "share" / "gatk4-4.6.2.0-1" / "gatk"
    assert calls == [["python", str(real_script), "HaplotypeCaller"]]


@pytest.mark.parametrize(
    "argv",
    [
        [r"C:\Users\lab user\.pixi\bin\pixi.exe", "run", "-c", "print('a b')"],
        [r"C:\Users\bob\pixi.exe", r"C:\a\b", 'q"uote', "trail\\", ""],
    ],
)
def test_windows_argv_split_inverts_list2cmdline(argv):
    import subprocess

    from wgsextract_cli.core.tool_commands import _split_windows_argv

    assert _split_windows_argv(subprocess.list2cmdline(argv)) == argv


@pytest.mark.parametrize("installed", [True, False])
def test_embedded_pixi_is_used_only_in_an_install_layout(tmp_path, installed):
    """install.sh puts pixi in <install>/.pixi beside app/; next to a plain
    checkout (~/wgsextract-cli) that path is the user's own ~/.pixi."""
    from wgsextract_cli.core import pixi_tools

    app = tmp_path / "app"
    app.mkdir()
    embedded = tmp_path / ".pixi" / "bin" / "pixi"
    embedded.parent.mkdir(parents=True)
    embedded.write_text("")
    if installed:
        (tmp_path / "install-manifest.json").write_text("{}")
    with (
        patch.object(pixi_tools.runtime, "source_checkout_root", return_value=app),
        patch.object(pixi_tools.runtime, "is_windows_host", return_value=False),
        patch.object(pixi_tools.shutil, "which", return_value="/usr/local/bin/pixi"),
    ):
        expected = str(embedded) if installed else "/usr/local/bin/pixi"
        assert pixi_tools._resolve_pixi_command() == expected
