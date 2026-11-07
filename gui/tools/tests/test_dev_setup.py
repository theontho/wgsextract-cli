"""Focused checks for shared setup prerequisite handling."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev_setup.py"
SPEC = importlib.util.spec_from_file_location("dev_setup", SCRIPT)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class DesktopPrerequisiteTests(unittest.TestCase):
    def test_smoke_preserves_existing_app_files_and_removes_private_workspace(
        self,
    ) -> None:
        node = setup.shutil.which("node")
        if not node:
            self.skipTest("Node is required for the smoke probe")
        variable = (
            "LOCALAPPDATA"
            if sys.platform == "win32"
            else "HOME"
            if sys.platform == "darwin"
            else "XDG_DATA_HOME"
        )
        (setup.ROOT / "tmp").mkdir(exist_ok=True)
        for failing in (False, True):
            with (
                self.subTest(failing=failing),
                tempfile.TemporaryDirectory(dir=setup.ROOT / "tmp") as directory,
            ):
                personal = Path(directory)
                original = personal / "personal-ui/state.json"
                original.parent.mkdir()
                original.write_text("keep", encoding="utf-8")
                source = (
                    'const fs = require("node:fs"), path = require("node:path");'
                    f"const root = process.env[{json.dumps(variable)}];"
                    "const name = process.env.GUI_FOR_CLI_APP_SUPPORT_NAME;"
                    'const marker = path.join(root, name, "state.json");'
                    "fs.mkdirSync(path.dirname(marker), {recursive:true});"
                    'fs.writeFileSync(marker, "smoke");'
                    'console.log("SMOKE_STATE " + JSON.stringify({root, name, marker}));'
                )
                source += (
                    "process.exit(7);"
                    if failing
                    else 'const server = require("node:http").createServer((req, res) => res.end("<html>probe</html>"));'
                    'server.listen(0, "127.0.0.1", () => console.log("Web UI: http://127.0.0.1:" + server.address().port + "/"));'
                )
                with patch.dict(
                    setup.os.environ,
                    {
                        "GUI_FOR_CLI_APP_SUPPORT_NAME": "personal-ui",
                        "HOME": str(personal),
                        "LOCALAPPDATA": str(personal),
                        "XDG_DATA_HOME": str(personal),
                    },
                ):
                    if failing:
                        with self.assertRaises(RuntimeError):
                            setup.smoke([node, "-e", source])
                    else:
                        setup.smoke([node, "-e", source])
                    self.assertEqual(
                        setup.os.environ["GUI_FOR_CLI_APP_SUPPORT_NAME"], "personal-ui"
                    )
                line = next(
                    line
                    for line in (setup.ROOT / "tmp/dev-setup/dev-smoke.log")
                    .read_text()
                    .splitlines()
                    if line.startswith("SMOKE_STATE ")
                )
                state = json.loads(line.removeprefix("SMOKE_STATE "))
                self.assertNotEqual(state["name"], "personal-ui")
                self.assertTrue(
                    Path(state["root"]).is_relative_to(setup.ROOT / "tmp/dev-setup")
                )
                self.assertFalse(Path(state["root"]).exists())
                self.assertFalse(Path(state["marker"]).exists())
                self.assertEqual(original.read_text(), "keep")

    def test_windows_sdk_needs_um_and_ucrt_from_the_same_version(self) -> None:
        (setup.ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=setup.ROOT / "tmp") as directory:
            root = Path(directory)
            kernel = root / "10.0.1/um/x64/kernel32.lib"
            other_ucrt = root / "10.0.2/ucrt/x64/ucrt.lib"
            for library in (kernel, other_ucrt):
                library.parent.mkdir(parents=True)
                library.touch()
            self.assertFalse(setup.windows_sdk_installed(root, "x64"))
            ucrt = root / "10.0.1/ucrt/x64/ucrt.lib"
            ucrt.parent.mkdir(parents=True)
            ucrt.touch()
            self.assertTrue(setup.windows_sdk_installed(root, "x64"))
            self.assertFalse(setup.windows_sdk_installed(root, "arm64"))

    def test_loopback_smoke_uses_latest_port_without_the_users_http_proxy(self) -> None:
        node = setup.shutil.which("node")
        if not node:
            self.skipTest("Node is required for the smoke probe")
        source = (
            'console.log("Web UI: http://127.0.0.1:0/");'
            'const http = require("node:http");'
            'const server = http.createServer((req, res) => res.end("<html>probe</html>"));'
            'server.listen(0, "127.0.0.1", () => console.log('
            '"Web UI: http://127.0.0.1:" + server.address().port + "/"));'
        )
        with patch.dict(
            setup.os.environ,
            {
                "http_proxy": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "no_proxy": "",
                "NO_PROXY": "",
            },
        ):
            setup.smoke([node, "-e", source])

    def test_missing_desktop_prerequisites_fail_unless_web_only_is_requested(
        self,
    ) -> None:
        for flags, expected in [([], 1), (["--web-only"], 0)]:
            with (
                self.subTest(flags=flags),
                patch.object(sys, "argv", ["dev_setup.py", "--skip-smoke", *flags]),
                patch.object(
                    setup,
                    "desktop_prerequisites",
                    return_value=["Install system libraries"],
                ),
                patch.object(setup, "run"),
                patch.object(setup, "succeeds", return_value=True),
                patch.object(setup, "smoke") as smoke,
            ):
                self.assertEqual(setup.main(), expected)
                smoke.assert_not_called()

    def test_linux_missing_webkit_prints_admin_commands(self) -> None:
        with (
            patch.object(setup.sys, "platform", "linux"),
            patch.object(setup, "succeeds", return_value=False),
        ):
            instructions = setup.desktop_prerequisites()
        self.assertTrue(any("libwebkit2gtk-4.1-dev" in item for item in instructions))
        self.assertTrue(any("sudo dnf install" in item for item in instructions))

    def test_linux_with_libraries_but_no_compiler_is_not_ready(self) -> None:
        with (
            patch.object(setup.sys, "platform", "linux"),
            patch.object(setup, "succeeds", return_value=True),
            patch.object(setup.shutil, "which", return_value=None),
        ):
            self.assertTrue(setup.desktop_prerequisites())

    def test_macos_needs_working_clang_not_only_xcode_path(self) -> None:
        with (
            patch.object(setup.sys, "platform", "darwin"),
            patch.object(setup, "succeeds", side_effect=[True, False]),
        ):
            self.assertEqual(
                setup.desktop_prerequisites(),
                ["macOS: xcode-select --install (accept the installer dialog)."],
            )


class CompileCacheAdviceTests(unittest.TestCase):
    """New worktrees rebuild every Tauri crate unless sccache can share them."""

    ROOT = Path("/work/src/wgsextract-cli-feature")
    SCCACHE = '[build]\nrustc-wrapper = "sccache"\n'

    def advice(
        self,
        cargo: dict[str, str],
        *,
        sccache: bool = True,
        sccache_config: str | None = None,
        env_basedirs: str | None = None,
        root: Path | None = None,
    ) -> str | None:
        (setup.ROOT / "tmp").mkdir(exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=setup.ROOT / "tmp") as directory,
            patch.dict(setup.os.environ, clear=False) as environ,
            patch.object(
                setup.shutil, "which", return_value="/bin/sccache" if sccache else None
            ),
        ):
            for name in ("RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER", "SCCACHE_BASEDIRS"):
                environ.pop(name, None)
            if env_basedirs is not None:
                environ["SCCACHE_BASEDIRS"] = env_basedirs
            home = Path(directory)
            for name, text in cargo.items():
                (home / name).write_text(text, encoding="utf-8")
            config = home / "sccache-config"
            if sccache_config is not None:
                config.write_text(sccache_config, encoding="utf-8")
            return setup.compile_cache_advice(home, root or self.ROOT, config)

    def basedirs(self, *dirs: Path) -> str:
        """A basedirs line as a user types it, Windows backslashes and all."""
        return "basedirs = [" + ", ".join(f"'{d}'" for d in dirs) + "]\n"

    def test_no_advice_once_sccache_strips_this_worktree_root(self) -> None:
        self.assertIsNone(
            self.advice({"config.toml": self.SCCACHE}, sccache_config=self.basedirs(self.ROOT))
        )

    def test_a_parent_folder_in_basedirs_covers_the_worktree(self) -> None:
        self.assertIsNone(
            self.advice(
                {"config.toml": self.SCCACHE},
                sccache_config=self.basedirs(Path("/elsewhere"), self.ROOT.parent),
            )
        )

    def test_a_wrapper_without_this_worktree_in_basedirs_is_not_enough(self) -> None:
        sibling = self.ROOT.parent / "wgsextract-cli"
        for config in (None, self.basedirs(sibling)):
            with self.subTest(config=config):
                advice = self.advice({"config.toml": self.SCCACHE}, sccache_config=config)
                assert advice is not None
                self.assertIn(f"basedirs = ['{self.ROOT}']", advice)

    def test_the_advised_basedirs_line_can_be_pasted_as_is(self) -> None:
        # Issue: a Windows path printed bare, pasted inside double quotes, is invalid TOML.
        # POSIX allows backslashes in a folder name, so this runs everywhere.
        windows_style = "C:\\Users\\me\\src"
        root = Path(windows_style) if setup.os.name == "nt" else Path("/work", windows_style)
        advice = self.advice({"config.toml": self.SCCACHE}, root=root)
        assert advice is not None
        line = re.search(r"basedirs = \[.*\]", advice)
        assert line is not None
        self.assertIsNone(
            self.advice({"config.toml": self.SCCACHE}, sccache_config=line.group() + "\n", root=root)
        )

    def test_reports_an_sccache_config_that_does_not_parse(self) -> None:
        advice = self.advice(
            {"config.toml": self.SCCACHE}, sccache_config='basedirs = ["C:\\Users\\me"]\n'
        )
        assert advice is not None
        self.assertIn("not valid TOML", advice)

    def test_sccache_basedirs_from_the_environment_replace_the_file(self) -> None:
        cargo = {"config.toml": self.SCCACHE}
        file_has_root = self.basedirs(self.ROOT)
        self.assertIsNotNone(
            self.advice(cargo, sccache_config=file_has_root, env_basedirs="/elsewhere")
        )
        self.assertIsNone(
            self.advice(
                cargo,
                sccache_config=None,
                env_basedirs=setup.os.pathsep.join(["/elsewhere", str(self.ROOT.parent)]),
            )
        )

    def test_advises_the_config_file_cargo_actually_reads(self) -> None:
        # Cargo ignores config.toml when the legacy `config` file exists.
        advice = self.advice(
            {"config.toml": self.SCCACHE, "config": "[net]\nretry = 3\n"},
            sccache_config=self.basedirs(self.ROOT),
        )
        assert advice is not None
        self.assertIn('rustc-wrapper = "sccache"', advice)
        self.assertRegex(advice, r"[/\\]config,")

    def test_no_advice_for_another_rustc_wrapper(self) -> None:
        self.assertIsNone(
            self.advice({"config.toml": '[build]\nrustc-wrapper = "/usr/bin/ccache"\n'})
        )

    def test_advises_installing_sccache_when_missing(self) -> None:
        advice = self.advice({}, sccache=False)
        assert advice is not None
        self.assertIn("cargo install sccache", advice)
        self.assertIn("config.toml", advice)
        self.assertIn("basedirs", advice)

    def test_advises_only_configuring_when_sccache_is_installed(self) -> None:
        for cargo in ({}, {"config.toml": "[net]\nretry = 3\n"}, {"config.toml": "not = [valid toml"}):
            with self.subTest(cargo=cargo):
                advice = self.advice(cargo)
                assert advice is not None
                self.assertIn('rustc-wrapper = "sccache"', advice)
                self.assertNotIn("cargo install", advice)


if __name__ == "__main__":
    unittest.main()
