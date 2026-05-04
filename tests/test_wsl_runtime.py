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
from wgsextract_cli.core.utils import _normalize_subprocess_cmd  # noqa: E402


class TestWSLRuntime(unittest.TestCase):
    def setUp(self):
        runtime.detect_wsl_available.cache_clear()
        runtime.wsl_command_available.cache_clear()
        runtime.wsl_pixi_tool_available.cache_clear()
        runtime.windows_to_wsl_path.cache_clear()

    def tearDown(self):
        runtime.detect_wsl_available.cache_clear()
        runtime.wsl_command_available.cache_clear()
        runtime.wsl_pixi_tool_available.cache_clear()
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
            patch("wgsextract_cli.core.runtime.should_consider_wsl", return_value=True),
            patch(
                "wgsextract_cli.core.runtime.wsl_command_available", return_value=True
            ),
        ):
            self.assertEqual(dependencies.get_tool_path("samtools"), "wsl:samtools")

    def test_get_tool_path_uses_wsl_pixi_when_tool_not_on_wsl_path(self):
        with (
            patch("wgsextract_cli.core.dependencies.shutil.which", return_value=None),
            patch("wgsextract_cli.core.runtime.should_consider_wsl", return_value=True),
            patch(
                "wgsextract_cli.core.runtime.wsl_command_available", return_value=False
            ),
            patch(
                "wgsextract_cli.core.runtime.wsl_pixi_tool_available",
                return_value=True,
            ),
        ):
            self.assertEqual(
                dependencies.get_tool_path("bcftools"),
                "wsl:~/.pixi/bin/pixi run -e default bcftools",
            )

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


if __name__ == "__main__":
    unittest.main()
