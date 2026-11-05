"""Focused checks for shared setup prerequisite handling."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/dev_setup.py"
SPEC = importlib.util.spec_from_file_location("dev_setup", SCRIPT)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)
REGISTER_SPEC = importlib.util.spec_from_file_location(
    "dev_register", SCRIPT.with_name("dev-register.py")
)
assert REGISTER_SPEC and REGISTER_SPEC.loader
register = importlib.util.module_from_spec(REGISTER_SPEC)
sys.modules[REGISTER_SPEC.name] = register
REGISTER_SPEC.loader.exec_module(register)


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

    def test_registration_preserves_local_identity_without_github_or_config_write(
        self,
    ) -> None:
        with (
            patch.object(
                sys,
                "argv",
                [
                    "dev-register.py",
                    "--local-only",
                    "--no-update-git-config",
                    "--choice",
                    "1",
                ],
            ),
            patch.object(
                register, "git_config", side_effect=["Developer", "dev@example.com"]
            ),
            patch.object(register, "available_identities") as github,
            patch.object(register, "run") as command,
            patch("builtins.open", mock_open()) as output,
        ):
            self.assertEqual(register.main(), 0)
        github.assert_not_called()
        command.assert_not_called()
        output().write.assert_called_once_with(
            "name=Developer\nemail=dev@example.com\n"
        )

    def test_select_identity_uses_default_without_a_usable_prompt(self) -> None:
        identities = [
            register.Identity("Git", "One", "one@example.com", None, False),
            register.Identity("GitHub", "Two", "two@example.com", "two", True),
        ]
        with (
            patch.object(register.sys, "stdin") as stdin,
            patch("builtins.input", side_effect=EOFError) as prompt,
        ):
            stdin.isatty.return_value = True
            self.assertIs(register.select_identity(identities, None), identities[1])
            prompt.assert_called_once()
            prompt.reset_mock()
            self.assertIs(
                register.select_identity(identities[:1], None), identities[0]
            )
            prompt.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
