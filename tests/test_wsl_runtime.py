import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core import (
    dependencies,  # noqa: E402
    runtime,  # noqa: E402
)
from wgsextract_cli.core.utils import (  # noqa: E402
    _normalize_subprocess_cmd,
    run_command,
)


class TestWSLRuntime(unittest.TestCase):
    def setUp(self):
        runtime.detect_wsl_available.cache_clear()
        runtime.wsl_command_available.cache_clear()
        runtime.wsl_pixi_tool_available.cache_clear()
        runtime.detect_bundled_runtime_available.cache_clear()
        runtime.bundled_command_available.cache_clear()
        runtime.pacman_tool_path.cache_clear()
        runtime.pacman_tool_available.cache_clear()
        runtime.windows_to_wsl_path.cache_clear()

    def tearDown(self):
        runtime.detect_wsl_available.cache_clear()
        runtime.wsl_command_available.cache_clear()
        runtime.wsl_pixi_tool_available.cache_clear()
        runtime.detect_bundled_runtime_available.cache_clear()
        runtime.bundled_command_available.cache_clear()
        runtime.pacman_tool_path.cache_clear()
        runtime.pacman_tool_available.cache_clear()
        runtime.windows_to_wsl_path.cache_clear()

    def test_get_tool_path_prefers_native_tool(self):
        with patch("wgsextract_cli.core.dependencies.shutil.which") as mock_which:
            mock_which.side_effect = lambda tool: (
                r"C:\tools\samtools.exe" if tool == "samtools" else None
            )

            self.assertEqual(
                dependencies.get_tool_path("samtools"), r"C:\tools\samtools.exe"
            )

    def test_get_tool_path_uses_wsl_tool_when_native_missing(self):
        with (
            patch("wgsextract_cli.core.dependencies.shutil.which", return_value=None),
            patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None),
            patch("wgsextract_cli.core.runtime.should_consider_wsl", return_value=True),
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode", return_value="auto"
            ),
            patch(
                "wgsextract_cli.core.runtime.wsl_command_available", return_value=True
            ),
        ):
            self.assertEqual(dependencies.get_tool_path("samtools"), "wsl:samtools")

    def test_get_tool_path_uses_wsl_pixi_when_tool_not_on_wsl_path(self):
        with (
            patch("wgsextract_cli.core.dependencies.shutil.which", return_value=None),
            patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None),
            patch("wgsextract_cli.core.runtime.should_consider_wsl", return_value=True),
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode", return_value="auto"
            ),
            patch(
                "wgsextract_cli.core.runtime.wsl_command_available", return_value=False
            ),
            patch(
                "wgsextract_cli.core.runtime.wsl_pixi_tool_available",
                return_value=True,
            ),
            patch(
                "wgsextract_cli.core.dependencies._wsl_home_dir",
                return_value="/home/test",
            ),
        ):
            self.assertEqual(
                dependencies.get_tool_path("bcftools"),
                "wsl:/home/test/.pixi/bin/pixi run -e default bcftools",
            )

    def test_get_tool_path_explicit_wsl_does_not_fall_back_to_native(self):
        with (
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode", return_value="wsl"
            ),
            patch("wgsextract_cli.core.runtime.should_consider_wsl", return_value=True),
            patch(
                "wgsextract_cli.core.runtime.wsl_command_available", return_value=False
            ),
            patch(
                "wgsextract_cli.core.runtime.wsl_pixi_tool_available",
                return_value=False,
            ),
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                return_value=r"C:\msys64\ucrt64\bin\samtools.exe",
            ),
        ):
            self.assertIsNone(dependencies.get_tool_path("samtools"))

    def test_get_tool_path_explicit_pacman_does_not_fall_back_to_native(self):
        with (
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode",
                return_value="pacman",
            ),
            patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None),
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                return_value=r"C:\tools\samtools.exe",
            ),
        ):
            self.assertIsNone(dependencies.get_tool_path("samtools"))

    def test_get_tool_path_uses_host_pixi_when_wsl_not_applicable(self):
        completed = MagicMock(returncode=0)
        with (
            patch(
                "wgsextract_cli.core.dependencies.shutil.which",
                side_effect=lambda tool: (
                    "/usr/local/bin/pixi" if tool == "pixi" else None
                ),
            ),
            patch(
                "wgsextract_cli.core.runtime.should_consider_wsl", return_value=False
            ),
            patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None),
            patch(
                "wgsextract_cli.core.dependencies.subprocess.run",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                dependencies.get_tool_path("samtools"),
                "/usr/local/bin/pixi run -e default samtools",
            )

    def test_get_tool_path_prefers_explicit_bundled_runtime(self):
        with (
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode",
                return_value="cygwin",
            ),
            patch(
                "wgsextract_cli.core.runtime.bundled_command_available",
                return_value=True,
            ),
            patch("wgsextract_cli.core.dependencies.shutil.which") as mock_which,
        ):
            self.assertEqual(dependencies.get_tool_path("samtools"), "cygwin:samtools")

        mock_which.assert_not_called()

    def test_get_tool_path_can_use_bundled_runtime_in_auto_mode(self):
        def bundled_available(mode: str, tool: str) -> bool:
            return mode == "msys2" and tool == "samtools"

        with (
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode", return_value="auto"
            ),
            patch("wgsextract_cli.core.runtime.is_windows_host", return_value=True),
            patch(
                "wgsextract_cli.core.runtime.should_consider_wsl", return_value=False
            ),
            patch("wgsextract_cli.core.dependencies.shutil.which", return_value=None),
            patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None),
            patch(
                "wgsextract_cli.core.runtime.bundled_command_available",
                side_effect=bundled_available,
            ),
        ):
            self.assertEqual(dependencies.get_tool_path("samtools"), "msys2:samtools")

    def test_get_tool_path_prefers_explicit_pacman_runtime(self):
        pacman_path = r"C:\msys64\ucrt64\bin\samtools.exe"
        with (
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode",
                return_value="pacman",
            ),
            patch(
                "wgsextract_cli.core.runtime.pacman_tool_path",
                return_value=pacman_path,
            ),
            patch("wgsextract_cli.core.dependencies.shutil.which") as mock_which,
        ):
            self.assertEqual(
                dependencies.get_tool_path("samtools"), f"pacman:{pacman_path}"
            )

        mock_which.assert_not_called()

    def test_get_tool_runtime_detects_pacman_path(self):
        self.assertEqual(
            dependencies.get_tool_runtime(r"C:\msys64\ucrt64\bin\samtools.exe"),
            "pacman",
        )

    def test_pacman_tool_path_finds_configured_ucrt64_dir(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bin_dir = Path(tempdir) / "ucrt64" / "bin"
            bin_dir.mkdir(parents=True)
            tool = bin_dir / "samtools.exe"
            tool.write_bytes(b"")

            with (
                patch("wgsextract_cli.core.runtime.shutil.which", return_value=None),
                patch(
                    "wgsextract_cli.core.runtime.pacman_ucrt64_bin_dirs",
                    return_value=[bin_dir],
                ),
            ):
                runtime.pacman_tool_path.cache_clear()
                self.assertEqual(
                    runtime.pacman_tool_path("samtools"), str(tool.resolve())
                )

    def test_default_runtime_root_uses_repo_runtime_for_development_checkout(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / ".git").mkdir()
            with patch("wgsextract_cli.core.runtime.repo_root", return_value=root):
                self.assertEqual(runtime.default_runtime_root(), root / "runtime")

    def test_default_runtime_root_uses_user_data_dir_for_installed_context(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "site-packages"
            data_dir = Path(tempdir) / "data"
            root.mkdir()
            with (
                patch("wgsextract_cli.core.runtime.repo_root", return_value=root),
                patch("platformdirs.user_data_path", return_value=data_dir),
            ):
                self.assertEqual(runtime.default_runtime_root(), data_dir / "runtime")

    def test_translate_windows_paths_without_touching_regions_flags_or_urls(self):
        with patch(
            "wgsextract_cli.core.runtime.detect_wsl_available", return_value=False
        ):
            self.assertEqual(
                runtime.translate_wsl_arg(r"C:\Users\mac\data\sample.bam"),
                "/mnt/c/Users/mac/data/sample.bam",
            )
            self.assertEqual(
                runtime.translate_wsl_arg(r"tmp\wsl_stress\data\fake.bam"),
                "tmp/wsl_stress/data/fake.bam",
            )
            self.assertEqual(
                runtime.translate_wsl_arg(r"OUT=tmp\wsl_stress\data\fake.bam"),
                "OUT=tmp/wsl_stress/data/fake.bam",
            )
            self.assertEqual(runtime.translate_wsl_arg("chrM:1-100"), "chrM:1-100")
            self.assertEqual(runtime.translate_wsl_arg("-o"), "-o")
            self.assertEqual(
                runtime.translate_wsl_arg("https://example.test/file.vcf.gz"),
                "https://example.test/file.vcf.gz",
            )
            self.assertEqual(runtime.translate_wsl_arg("16GB"), "16GB")

    def test_wrap_command_quotes_wsl_script_and_cwd(self):
        with (
            patch(
                "wgsextract_cli.core.runtime.os.getcwd", return_value=r"C:\repo root"
            ),
            patch(
                "wgsextract_cli.core.runtime.windows_to_wsl_path",
                side_effect=lambda path: path.replace("C:\\", "/mnt/c/").replace(
                    "\\", "/"
                ),
            ),
        ):
            wrapped = runtime.wrap_command(
                ["wsl:samtools", "view", r"C:\data dir\sample.bam"],
            )

        self.assertEqual(wrapped[:3], ["wsl", "bash", "-lc"])
        self.assertIn("cd '/mnt/c/repo root'", wrapped[3])
        self.assertIn("samtools view", wrapped[3])
        self.assertIn("'/mnt/c/data dir/sample.bam'", wrapped[3])

    def test_wrap_command_uses_bundled_runtime_shell(self):
        with (
            patch(
                "wgsextract_cli.core.runtime.os.getcwd", return_value=r"C:\repo root"
            ),
            patch(
                "wgsextract_cli.core.runtime.runtime_root",
                return_value=Path(r"C:\repo root\runtime"),
            ),
        ):
            wrapped = runtime.wrap_command(
                ["msys2:samtools", "view", r"C:\data dir\sample.bam"],
            )

        self.assertEqual(wrapped[1], "-lc")
        self.assertTrue(
            str(wrapped[0]).replace("\\", "/").endswith("msys2/usr/bin/bash.exe")
        )
        self.assertIn("MSYSTEM=UCRT64", wrapped[2])
        self.assertIn("/jre8/bin", wrapped[2])
        self.assertIn("/FastQC", wrapped[2])
        self.assertIn("cd 'C:/repo root'", wrapped[2])
        self.assertIn("samtools view", wrapped[2])
        self.assertIn("'C:/data dir/sample.bam'", wrapped[2])

    def test_wrap_command_uses_pacman_direct_executable(self):
        wrapped = runtime.wrap_command(
            [
                "pacman:C:\\msys64\\ucrt64\\bin\\samtools.exe",
                "view",
                r"C:\data\sample.bam",
            ],
        )

        self.assertEqual(
            wrapped,
            [r"C:\msys64\ucrt64\bin\samtools.exe", "view", r"C:\data\sample.bam"],
        )

    def test_normalize_subprocess_cmd_wraps_wsl_tool_resolution(self):
        with (
            patch("wgsextract_cli.core.utils.shutil.which", return_value=None),
            patch(
                "wgsextract_cli.core.dependencies.get_tool_path",
                return_value="wsl:samtools",
            ),
            patch("wgsextract_cli.core.runtime.os.getcwd", return_value=r"C:\repo"),
            patch(
                "wgsextract_cli.core.runtime.windows_to_wsl_path",
                side_effect=lambda path: path.replace("C:\\", "/mnt/c/").replace(
                    "\\", "/"
                ),
            ),
        ):
            normalized = _normalize_subprocess_cmd(
                ["samtools", "idxstats", r"C:\data\a.bam"]
            )

        self.assertEqual(normalized[:3], ["wsl", "bash", "-lc"])
        self.assertIn("samtools idxstats /mnt/c/data/a.bam", normalized[3])

    def test_normalize_subprocess_cmd_wraps_pacman_tool_resolution(self):
        pacman_path = r"C:\msys64\ucrt64\bin\samtools.exe"
        with (
            patch("wgsextract_cli.core.utils.shutil.which", return_value=None),
            patch(
                "wgsextract_cli.core.dependencies.get_tool_path",
                return_value=f"pacman:{pacman_path}",
            ),
        ):
            normalized = _normalize_subprocess_cmd(
                ["samtools", "idxstats", r"C:\data\a.bam"]
            )

        self.assertEqual(
            normalized,
            [pacman_path, "idxstats", r"C:\data\a.bam"],
        )

    def test_normalize_subprocess_cmd_keeps_existing_executable_with_spaces(self):
        executable = r"C:\Program Files\Tool Suite\tool.exe"
        with patch("wgsextract_cli.core.utils.os.path.exists", return_value=True):
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

    def test_write_wslconfig_settings_adds_and_updates_wsl2_section(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / ".wslconfig"
            runtime.write_wslconfig_settings(
                memory="24GB", processors=8, swap="16GB", path=config_path
            )
            self.assertEqual(
                runtime.read_wslconfig_settings(config_path),
                {"memory": "24GB", "processors": "8", "swap": "16GB"},
            )

            runtime.write_wslconfig_settings(memory="32GB", path=config_path)
            settings = runtime.read_wslconfig_settings(config_path)
            self.assertEqual(settings["memory"], "32GB")
            self.assertEqual(settings["processors"], "8")

    def test_read_wslconfig_settings_handles_utf16(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / ".wslconfig"
            config_path.write_text(
                "[wsl2]\nmemory=24GB\nprocessors=8\n", encoding="utf-16"
            )

            self.assertEqual(
                runtime.read_wslconfig_settings(config_path),
                {"memory": "24GB", "processors": "8"},
            )

    def test_recommend_wslconfig_settings_uses_benchmark_ratios(self):
        recommendation = runtime.recommend_wslconfig_settings(
            host_processors=12,
            host_memory_bytes=64 * 1024**3,
        )

        self.assertEqual(recommendation.processors, 8)
        self.assertEqual(recommendation.memory, "48GB")
        self.assertEqual(recommendation.swap, "16GB")
        self.assertEqual(recommendation.host_processors, 12)
        self.assertEqual(recommendation.host_memory_gb, 64)

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

    def test_default_thread_policy_uses_wsl_balanced_count(self):
        with (
            patch.object(runtime, "macos_performance_core_count", return_value=None),
            patch.object(runtime, "should_consider_wsl", return_value=True),
            patch.object(runtime.os, "cpu_count", return_value=16),
        ):
            profile = runtime.default_thread_tuning_profile()

        self.assertEqual(profile.threads, 12)
        self.assertEqual(profile.reason, "WSL balanced CPU allocation")

    def test_default_thread_policy_falls_back_to_all_cores(self):
        with (
            patch.object(runtime, "macos_performance_core_count", return_value=None),
            patch.object(runtime, "should_consider_wsl", return_value=False),
            patch.object(runtime.os, "cpu_count", return_value=16),
        ):
            profile = runtime.default_thread_tuning_profile()

        self.assertEqual(profile.threads, 16)
        self.assertEqual(profile.reason, "all available cores")

    def test_detect_wsl_available_uses_wsl_probe(self):
        completed = MagicMock(returncode=0, stdout="ok", stderr="")
        with (
            patch.object(runtime.sys, "platform", "win32"),
            patch("wgsextract_cli.core.runtime.shutil.which", return_value="wsl.exe"),
            patch(
                "wgsextract_cli.core.runtime.subprocess.run", return_value=completed
            ) as mock_run,
        ):
            self.assertTrue(runtime.detect_wsl_available())

        mock_run.assert_called_once()

    def test_detect_wsl_available_force_ignores_runtime_policy(self):
        completed = MagicMock(returncode=0, stdout="ok", stderr="")
        with (
            patch(
                "wgsextract_cli.core.runtime.should_consider_wsl", return_value=False
            ),
            patch("wgsextract_cli.core.runtime.shutil.which", return_value="wsl.exe"),
            patch(
                "wgsextract_cli.core.runtime.subprocess.run", return_value=completed
            ) as mock_run,
        ):
            self.assertFalse(runtime.detect_wsl_available())
            self.assertTrue(runtime.detect_wsl_available(force=True))

        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
