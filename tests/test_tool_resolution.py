"""Where tools come from: PATH (the Pixi environment) first, then the tool's
own Pixi environment, the same on Windows as on macOS and Linux."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core import (  # noqa: E402
    config,
    dependencies,
    pixi_tools,
    runtime,
)
from wgsextract_cli.core.paths import expand_path  # noqa: E402
from wgsextract_cli.core.tool_catalog import JAVA_JAR  # noqa: E402
from wgsextract_cli.core.utils import (  # noqa: E402
    _normalize_subprocess_cmd,
    run_command,
)


@pytest.fixture(autouse=True)
def _outside_a_pixi_environment(monkeypatch):
    """These tests simulate the Pixi environment probe; under `pixi run` the CLI
    would look in its own environment first instead."""
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)


PIXI_EXE = r"C:\Users\test\.pixi\bin\pixi.exe"


@pytest.mark.parametrize("windows", [True, False])
def test_gatk_direct_java_preserves_launcher_heap_and_worker_options(
    monkeypatch, tmp_path, windows
):
    monkeypatch.setattr(runtime, "is_windows_host", lambda: windows)
    command = [
        tmp_path / "gatk env" / "java.exe",
        "-Dsamjdk.compression_level=2",
        "-cp", r"C:\gatk env\sidecar.jar;C:\gatk env\gatk.jar",
        "org.broadinstitute.hellbender.Main",
        "--java-options",
        "-Xmx4g -XX:ActiveProcessorCount=1 -XX:ParallelGCThreads=1",
        "HaplotypeCaller", "-I", r"C:\sample folder\sample.bam",
    ]  # fmt: skip
    actual = _normalize_subprocess_cmd(command)
    if windows:
        main_at = actual.index("org.broadinstitute.hellbender.Main")
        assert "--java-options" not in actual
        assert actual[main_at - 3 : main_at] == [
            "-Xmx4g",
            "-XX:ActiveProcessorCount=1",
            "-XX:ParallelGCThreads=1",
        ]
        assert actual[-1] == r"C:\sample folder\sample.bam"
        assert actual[actual.index("-cp") + 1] == command[command.index("-cp") + 1]
    else:
        assert actual == [str(argument) for argument in command]


def _which_only_pixi(tool: str) -> str | None:
    return PIXI_EXE if tool == "pixi" else None


class TestToolResolution(unittest.TestCase):
    def setUp(self):
        dependencies.clear_tool_caches()
        self.addCleanup(dependencies.clear_tool_caches)
        # These tests compare where a tool resolves; the manifest is incidental.
        manifest_patch = patch(
            "wgsextract_cli.core.pixi_tools._pixi_manifest_args", return_value=[]
        )
        manifest_patch.start()
        self.addCleanup(manifest_patch.stop)

    def test_windows_tool_in_pixi_library_bin_resolves_from_path(self):
        # Inside `pixi run` on Windows, the env's Library\bin is on PATH.
        samtools = r"C:\app\.pixi\envs\default\Library\bin\samtools.exe"
        with (
            patch("wgsextract_cli.core.runtime.is_windows_host", return_value=True),
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=lambda tool: samtools if tool == "samtools" else None,
            ),
            patch("wgsextract_cli.core.pixi_tools.subprocess.run") as probe,
        ):
            self.assertEqual(dependencies.get_tool_path("samtools"), samtools)
        probe.assert_not_called()

    def test_tool_off_path_resolves_through_its_pixi_environment(self):
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
                dependencies.get_tool_path("samtools"),
                "/usr/local/bin/pixi run -e default samtools",
            )

    def test_windows_alt_env_tool_resolves_through_pixi(self):
        completed = MagicMock(returncode=0, stdout="direct-name:gatk\n")
        with (
            patch("wgsextract_cli.core.runtime.is_windows_host", return_value=True),
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=_which_only_pixi,
            ),
            patch(
                "wgsextract_cli.core.pixi_tools.shutil.which",
                side_effect=_which_only_pixi,
            ),
            patch.dict(os.environ, {"PIXI_EXE": ""}),
            patch(
                "wgsextract_cli.core.pixi_tools.subprocess.run",
                return_value=completed,
            ) as probe,
        ):
            self.assertEqual(
                dependencies.get_tool_path("gatk"),
                rf"{PIXI_EXE} run -e gatk gatk",
            )
        self.assertEqual(
            probe.call_args.args[0][:6],
            [PIXI_EXE, "run", "--no-install", "-e", "gatk", "python"],
        )

    def test_windows_launches_pixi_noarch_script(self):
        completed = MagicMock(
            returncode=0,
            stdout=r"script:C:\repo\.pixi\envs\default\opt\fastqc-0.13.0\fastqc" + "\n",
        )
        with (
            patch("wgsextract_cli.core.runtime.is_windows_host", return_value=True),
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=_which_only_pixi,
            ),
            patch(
                "wgsextract_cli.core.pixi_tools.subprocess.run",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                dependencies.get_tool_path("fastqc"),
                rf"{PIXI_EXE} run -e default python C:\repo\.pixi\envs\default\opt\fastqc-0.13.0\fastqc",
            )

    def test_windows_launches_haplogrep_jar(self):
        completed = MagicMock(
            returncode=0,
            stdout=r"haplogrep:C:\repo\.pixi\envs\default\bin\haplogrep.jar" + "\n",
        )
        with (
            patch("wgsextract_cli.core.runtime.is_windows_host", return_value=True),
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=_which_only_pixi,
            ),
            patch(
                "wgsextract_cli.core.pixi_tools.subprocess.run",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                dependencies.get_tool_path("haplogrep"),
                f"{PIXI_EXE} run -e default {' '.join(JAVA_JAR)} "
                r"C:\repo\.pixi\envs\default\bin\haplogrep.jar",
            )

    def test_resolve_pixi_command_checks_windows_exe_fallback(self):
        own = expand_path("~/.pixi/bin/pixi.exe")

        def exists(path):
            return Path(path) == Path(own)

        with (
            patch.dict(os.environ, {"PIXI_EXE": ""}),
            patch("wgsextract_cli.core.pixi_tools.shutil.which", return_value=None),
            patch(
                "wgsextract_cli.core.pixi_tools.Path.exists",
                autospec=True,
                side_effect=exists,
            ),
        ):
            self.assertEqual(pixi_tools._resolve_pixi_command(), own)

    def test_get_tool_runtime_detects_pixi_env_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prefix = Path(tempdir) / "envs" / "default"
            tool = prefix / "Library" / "bin" / "samtools.exe"
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"")

            with patch.dict(
                os.environ,
                {"CONDA_PREFIX": str(prefix), "PIXI_PROJECT_ROOT": str(Path(tempdir))},
                clear=True,
            ):
                self.assertEqual(dependencies.get_tool_runtime(str(tool)), "pixi")

    def test_get_tool_runtime_does_not_treat_plain_conda_env_as_pixi(self):
        with tempfile.TemporaryDirectory() as tempdir:
            prefix = Path(tempdir) / "envs" / "default"
            tool = prefix / "bin" / "python"
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"")

            with patch.dict(os.environ, {"CONDA_PREFIX": str(prefix)}, clear=True):
                self.assertEqual(dependencies.get_tool_runtime(str(tool)), "native")

    def test_get_tool_runtime_detects_pixi_run_command(self):
        self.assertEqual(
            dependencies.get_tool_runtime("/usr/local/bin/pixi run -e default java"),
            "pixi",
        )
        self.assertEqual(dependencies.get_tool_runtime(None), "missing")

    def test_normalize_subprocess_cmd_keeps_existing_executable_with_spaces(self):
        executable = r"C:\Program Files\Tool Suite\tool.exe"
        with patch("wgsextract_cli.core.utils.Path.exists", return_value=True):
            normalized = _normalize_subprocess_cmd([executable, "--version"])

        self.assertEqual(normalized, [executable, "--version"])

    def test_run_command_starts_managed_process_group(self):
        process = MagicMock()
        process.communicate.return_value = ("", "")
        process.returncode = 0

        with patch(
            "wgsextract_cli.core.utils.subprocess.Popen", return_value=process
        ) as popen:
            run_command(["tool", "--version"])

        kwargs = popen.call_args.kwargs
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"])
        else:
            self.assertTrue(kwargs["start_new_session"])


class TestLegacyRuntimeSettings(unittest.TestCase):
    """Older Windows installers and the app wrote these; they must not break."""

    LEGACY = (
        'tool_runtime = "windows"\n'
        'pacman_ucrt64_bin = "C:/msys64/ucrt64/bin"\n'
        'runtime_directory = "C:/wgse/runtime"\n'
        'runtime = "wsl"\n'
        'pacman_bin = "C:/msys64/ucrt64/bin"\n'
    )

    def test_legacy_runtime_settings_are_ignored_on_load(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.toml"
            path.write_text(self.LEGACY + 'cpu_threads = "4"\n')
            config.set_config_path(path)
            try:
                with self.assertNoLogs(level="WARNING"):
                    loaded = config.load_config()
            finally:
                config.set_config_path(None)
        self.assertEqual(loaded, {"cpu_threads": "4"})

    def test_saving_config_drops_legacy_runtime_settings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.toml"
            path.write_text(self.LEGACY)
            config.set_config_path(path)
            try:
                config.save_config({"cpu_threads": "4"})
                text = path.read_text()
            finally:
                config.set_config_path(None)
                config.reload_settings()
        self.assertEqual(text.strip(), 'cpu_threads = "4"')


class TestSourceCheckout(unittest.TestCase):
    def _checkout_root_for(self, module_file: Path) -> Path | None:
        runtime.source_checkout_root.cache_clear()
        try:
            with patch.object(runtime, "__file__", str(module_file)):
                return runtime.source_checkout_root()
        finally:
            runtime.source_checkout_root.cache_clear()

    def test_source_checkout_root_finds_this_project(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir).resolve()
            module = root / "src" / "wgsextract_cli" / "core" / "runtime.py"
            module.parent.mkdir(parents=True)
            (root / "pyproject.toml").write_text('[project]\nname = "wgsextract-cli"\n')
            self.assertEqual(self._checkout_root_for(module), root)

    def test_source_checkout_root_ignores_enclosing_foreign_project(self):
        with tempfile.TemporaryDirectory() as tempdir:
            other_repo = Path(tempdir).resolve()
            (other_repo / ".git").mkdir()
            (other_repo / "pyproject.toml").write_text('[project]\nname = "other"\n')
            module = (
                other_repo
                / ".venv/lib/python3.12/site-packages/wgsextract_cli/core/runtime.py"
            )
            module.parent.mkdir(parents=True)
            self.assertIsNone(self._checkout_root_for(module))


class TestThreadPolicy(unittest.TestCase):
    def test_default_thread_policy_uses_apple_silicon_performance_cores(self):
        completed = MagicMock(
            returncode=0,
            stdout=(
                'hw.perflevel0.name: "Efficiency"\n'
                "hw.perflevel0.physicalcpu: 4\n"
                'hw.perflevel1.name: "Performance"\n'
                "hw.perflevel1.physicalcpu: 8\n"
                'hw.perflevel2.name: "Super"\n'
                "hw.perflevel2.physicalcpu: 2\n"
            ),
            stderr="",
        )
        with (
            patch.object(runtime.platform, "system", return_value="Darwin"),
            patch.object(runtime.platform, "machine", return_value="arm64"),
            patch.object(runtime.subprocess, "run", return_value=completed),
        ):
            profile = runtime.default_thread_tuning_profile()

        self.assertEqual(profile.threads, 10)
        self.assertEqual(profile.reason, "Apple Silicon performance-core count")

    def test_macos_performance_core_count_falls_back_to_perflevel0_count(self):
        missing_names = MagicMock(returncode=0, stdout="", stderr="")
        perflevel0_count = MagicMock(returncode=0, stdout="8\n", stderr="")
        with (
            patch.object(runtime.platform, "system", return_value="Darwin"),
            patch.object(runtime.platform, "machine", return_value="arm64"),
            patch.object(
                runtime.subprocess,
                "run",
                side_effect=[missing_names, perflevel0_count],
            ),
        ):
            core_count = runtime.macos_performance_core_count()

        self.assertEqual(core_count, 8)

    def test_default_thread_policy_uses_all_cores_elsewhere(self):
        with (
            patch.object(runtime, "macos_performance_core_count", return_value=None),
            patch.object(runtime.os, "cpu_count", return_value=16),
        ):
            profile = runtime.default_thread_tuning_profile()

        self.assertEqual(profile.threads, 16)
        self.assertEqual(profile.reason, "all available cores")


if __name__ == "__main__":
    unittest.main()
