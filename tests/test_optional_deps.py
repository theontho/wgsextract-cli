import os
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.commands import deps as deps_command  # noqa: E402
from wgsextract_cli.core.dependencies import verify_dependencies  # noqa: E402


class TestOptionalDependencies(unittest.TestCase):
    """Tests the new optional dependency handling."""

    @patch("wgsextract_cli.core.dependencies.get_tool_path")
    @patch("wgsextract_cli.core.dependencies.get_jar_dir", return_value="/tmp")
    @patch("sys.exit")
    @patch("logging.error")
    @patch("logging.info")
    def test_verify_optional_missing(
        self, mock_info, mock_error, mock_exit, mock_jar, mock_tool_path
    ):
        # Simulate 'minimap2' (optional) is missing
        mock_tool_path.side_effect = lambda x: (
            None if x == "minimap2" else "/usr/bin/" + x
        )

        try:
            verify_dependencies(["minimap2"])
        except SystemExit:
            pass

        # Check that it called logging.error with the optional message
        mock_error.assert_any_call(
            "Required optional tool(s) missing for this feature:"
        )
        mock_error.assert_any_call(" - minimap2")

        # Check for generic installation advice
        args, _ = mock_info.call_args
        self.assertIn("package manager", args[0])
        self.assertIn("brew, apt, conda", args[0])

        mock_exit.assert_called_with(1)

    @patch("wgsextract_cli.core.dependencies.get_tool_path")
    @patch("wgsextract_cli.core.dependencies.get_jar_dir", return_value="/tmp")
    @patch("sys.exit")
    @patch("logging.error")
    @patch("wgsextract_cli.core.dependencies.sys.platform", "linux")
    def test_verify_mandatory_missing(
        self, mock_error, mock_exit, mock_jar, mock_tool_path
    ):
        # Simulate 'samtools' (mandatory) is missing
        mock_tool_path.side_effect = lambda x: (
            None if x == "samtools" else "/usr/bin/" + x
        )

        try:
            verify_dependencies(["samtools"])
        except SystemExit:
            pass

        # Check that it called logging.error with the fatal message
        mock_error.assert_any_call(
            "Fatal Error: Missing required core tools or JAR files."
        )
        mock_error.assert_any_call(" - samtools")
        mock_exit.assert_called_with(1)

    @patch("wgsextract_cli.core.dependencies.sys.platform", "win32")
    @patch("wgsextract_cli.core.dependencies.get_tool_path", return_value=None)
    @patch("wgsextract_cli.core.dependencies.get_jar_dir", return_value="/tmp")
    @patch("logging.warning")
    def test_verify_mandatory_missing_on_windows_mentions_runtime_options(
        self, mock_warning, mock_jar, mock_tool_path
    ):
        verify_dependencies(["samtools"])

        warning_messages = [call.args[0] for call in mock_warning.call_args_list]
        self.assertTrue(any("deps wsl check" in msg for msg in warning_messages))
        self.assertTrue(any("deps cygwin setup" in msg for msg in warning_messages))
        self.assertTrue(any("deps msys2 setup" in msg for msg in warning_messages))
        self.assertTrue(any("deps pacman check" in msg for msg in warning_messages))

    def test_find_local_runtime_archive_uses_newest_matching_zip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            archive_dir = Path(tempdir)
            older = archive_dir / "msys2_old.zip"
            newer = archive_dir / "msys2_new.zip"
            older.write_bytes(b"older")
            newer.write_bytes(b"newer")
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))

            self.assertEqual(
                deps_command._find_local_runtime_archive("msys2", archive_dir), newer
            )

    def test_resolve_runtime_archive_reuses_cache_without_download(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache_dir = Path(tempdir)
            cached = cache_dir / "msys2_v1.zip"
            with zipfile.ZipFile(cached, "w") as archive:
                archive.writestr("msys2/usr/bin/bash.exe", "")
            args = Namespace(
                archive_dir=None,
                cache_dir=str(cache_dir),
                url="https://example.test/msys2_v1.zip",
                latest_json_url=deps_command.DEFAULT_WINDOWS_RUNTIME_RELEASE_URL,
                refresh_download=False,
            )

            with patch.object(deps_command, "_download_file") as mock_download:
                self.assertEqual(
                    deps_command._resolve_bundled_runtime_archive(args, "msys2"),
                    cached.resolve(),
                )

            mock_download.assert_not_called()

    def test_safe_extract_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tempdir:
            archive_path = Path(tempdir) / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "bad")

            with zipfile.ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(Exception, "unsafe path"):
                    deps_command._safe_extract_zip(archive, Path(tempdir) / "runtime")

    def test_copy_bundled_runtime_from_parent_source_dir(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "legacy"
            bash = source / "msys2" / "usr" / "bin" / "bash.exe"
            bash.parent.mkdir(parents=True)
            bash.write_bytes(b"")
            fastqc = source / "FastQC" / "fastqc"
            fastqc.parent.mkdir(parents=True)
            fastqc.write_text(
                "if ( $java_bin ne 'java' ) {\r\n"
                '    system $java_bin, @java_args, "-jar $RealBin/FastQC.jar", @files;\r\n'
                "}\r\n"
                "else {\r\n"
                '    exec $java_bin, @java_args, "-jar $RealBin/FastQC.jar", @files;\r\n'
                "}\r\n",
                encoding="utf-8",
            )
            java = source / "jre8" / "bin" / "java.exe"
            java.parent.mkdir(parents=True)
            java.write_bytes(b"")
            destination = root / "runtime" / "msys2"

            deps_command._copy_bundled_runtime_from_source(source, "msys2", destination)

            self.assertTrue((destination / "usr" / "bin" / "bash.exe").exists())
            self.assertTrue((destination / "FastQC" / "fastqc").exists())
            self.assertTrue((destination / "jre8" / "bin" / "java.exe").exists())
            self.assertIn(
                '"-jar", $fastqc_jar',
                (destination / "FastQC" / "fastqc").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "cygpath -w",
                (destination / "FastQC" / "fastqc").read_text(encoding="utf-8"),
            )

    def test_copy_bundled_runtime_skips_cygwin_mount_placeholder(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "legacy" / "cygwin64"
            bash = source / "bin" / "bash.exe"
            bash.parent.mkdir(parents=True)
            bash.write_bytes(b"")
            (source / "mnt").write_text("placeholder", encoding="utf-8")
            destination = root / "runtime" / "cygwin64"

            deps_command._copy_bundled_runtime_from_source(
                source, "cygwin", destination
            )

            self.assertTrue((destination / "bin" / "bash.exe").exists())
            self.assertFalse((destination / "mnt").exists())

    def test_copy_bundled_runtime_requires_expected_shell(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaisesRegex(Exception, "expected shell"):
                deps_command._copy_bundled_runtime_from_source(
                    Path(tempdir), "cygwin", Path(tempdir) / "runtime" / "cygwin64"
                )

    def test_check_dependencies_with_runtime_restores_env(self):
        env_name = deps_command.runtime.RUNTIME_ENV_VAR
        previous = os.environ.get(env_name)
        os.environ[env_name] = "auto"
        try:
            with patch.object(
                deps_command, "check_all_dependencies", return_value={}
            ) as mock_check:
                deps_command._check_dependencies_with_runtime("wsl")

            mock_check.assert_called_once_with()
            self.assertEqual(os.environ[env_name], "auto")
        finally:
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous

    @patch("wgsextract_cli.core.dependencies.shutil.which", return_value=None)
    @patch("wgsextract_cli.core.runtime.should_consider_wsl", return_value=True)
    @patch("wgsextract_cli.core.runtime.get_tool_runtime_mode", return_value="auto")
    @patch("wgsextract_cli.core.runtime.wsl_command_available", return_value=True)
    @patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None)
    def test_get_tool_path_can_return_wsl_fallback(
        self,
        mock_pacman,
        mock_wsl_available,
        mock_runtime_mode,
        mock_should_consider,
        mock_which,
    ):
        from wgsextract_cli.core.dependencies import get_tool_path

        self.assertEqual(get_tool_path("samtools"), "wsl:samtools")

    def test_get_tool_path_can_return_pixi_fallback_without_wsl(self):
        from wgsextract_cli.core.dependencies import get_tool_path

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
            patch(
                "wgsextract_cli.core.runtime.get_tool_runtime_mode",
                return_value="auto",
            ),
            patch("wgsextract_cli.core.runtime.pacman_tool_path", return_value=None),
            patch(
                "wgsextract_cli.core.dependencies.subprocess.run",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                get_tool_path("samtools"),
                "/usr/local/bin/pixi run -e default samtools",
            )

    def test_version_output_filters_wsl_mount_warning(self):
        from wgsextract_cli.core.dependencies import _version_output

        output = _version_output(
            "wsl: Failed to mount F:\\, see dmesg for more details.\n",
            "fastp 0.23.4\n",
        )

        self.assertEqual(output, "fastp 0.23.4")

    @patch("wgsextract_cli.core.dependencies.os.path.exists", return_value=True)
    def test_native_windows_tool_path_is_not_shlex_split(self, mock_exists):
        from wgsextract_cli.core.dependencies import _tool_command_parts

        tool_path = r"C:\Windows\system32\tar.EXE"

        self.assertEqual(_tool_command_parts(tool_path), [tool_path])

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

    @patch("wgsextract_cli.core.dependencies.MANDATORY_TOOLS", ["samtools"])
    @patch("wgsextract_cli.core.dependencies.OPTIONAL_TOOLS", ["minimap2"])
    @patch("wgsextract_cli.core.dependencies.get_tool_path")
    @patch("wgsextract_cli.core.dependencies.get_tool_version", return_value="1.0")
    def test_deps_check_output(self, mock_version, mock_tool_path):
        from wgsextract_cli.commands.deps import run

        # samtools present, minimap2 missing
        mock_tool_path.side_effect = lambda x: (
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

    def test_wsl_tune_uses_heuristic_defaults_with_overrides(self):
        from wgsextract_cli.commands.deps import run_wsl_tune
        from wgsextract_cli.core.runtime import WSLResourceRecommendation

        recommendation = WSLResourceRecommendation(
            memory="48GB",
            processors=8,
            swap="16GB",
            host_memory_gb=64,
            host_processors=12,
        )

        with (
            patch(
                "wgsextract_cli.commands.deps.runtime.recommend_wslconfig_settings",
                return_value=recommendation,
            ),
            patch(
                "wgsextract_cli.commands.deps.runtime.write_wslconfig_settings",
                return_value="C:/Users/test/.wslconfig",
            ) as mock_write,
            patch("sys.stdout", new=StringIO()) as fake_out,
        ):
            run_wsl_tune(Namespace(memory=None, processors=6, swap=None))

        mock_write.assert_called_once_with(memory="48GB", processors=6, swap="16GB")
        output = fake_out.getvalue()
        self.assertIn("processors=2/3", output)
        self.assertIn("memory=48GB, processors=6, swap=16GB", output)


if __name__ == "__main__":
    unittest.main()
