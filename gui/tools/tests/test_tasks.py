from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tasks  # noqa: E402


class TaskResolutionTests(unittest.TestCase):
    def test_build_defaults_to_web(self) -> None:
        self.assertEqual(tasks.resolve("build", None), [["npm", "--prefix", tasks.APP_DIR, "run", "build"]])

    def test_run_web_passes_bundle_and_port(self) -> None:
        with mock.patch.dict("os.environ", {"BUNDLE": "examples/GitHelper", "PORT": "9000"}):
            [command] = tasks.resolve("run", "web")
        self.assertEqual(command[-4:-2], ["--bundle", str(Path("examples/GitHelper").resolve())])
        self.assertEqual(command[-2:], ["--port", "9000"])

    def test_lint_validates_every_example_bundle(self) -> None:
        validate = next(command for command in tasks.lint("") if "bundle-validate-cli.js" in " ".join(command))
        self.assertIn("examples/WGSExtract", validate)
        self.assertIn("tests/conformance/basic-bundle", validate)

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            tasks.resolve("build", "swiftui")

    def test_targetless_action_rejects_target(self) -> None:
        with self.assertRaises(SystemExit):
            tasks.resolve("lint", "web")

    def test_ci_runs_lint_then_tests(self) -> None:
        commands = tasks.resolve("ci", None)
        self.assertEqual(commands[: len(tasks.lint(""))], tasks.lint(""))
        self.assertEqual(commands[len(tasks.lint("")) :], tasks.test(""))

    def test_windows_package_uses_powershell_script(self) -> None:
        with mock.patch.object(tasks, "IS_WINDOWS", True):
            [command] = tasks.resolve("package", "tauri")
        self.assertEqual(command[-1], "tools/packaging/windows/package_tauri.ps1")


if __name__ == "__main__":
    unittest.main()
