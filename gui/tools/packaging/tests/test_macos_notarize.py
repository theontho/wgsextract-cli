from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "posix"))
import macos_notarize  # noqa: E402

API_KEY_ENV = {"APPLE_API_KEY": "KEY123", "APPLE_API_ISSUER": "issuer-uuid", "APPLE_API_KEY_PATH": "/tmp/AuthKey.p8"}


class NotarytoolCredentialTests(unittest.TestCase):
    def test_prefers_the_api_key(self) -> None:
        env = {**API_KEY_ENV, "APPLE_ID": "me@example.com", "APPLE_PASSWORD": "pw", "APPLE_TEAM_ID": "TEAM"}
        self.assertEqual(
            macos_notarize.notarytool_credentials(env),
            ["--key", "/tmp/AuthKey.p8", "--key-id", "KEY123", "--issuer", "issuer-uuid"],
        )

    def test_falls_back_to_apple_id(self) -> None:
        env = {"APPLE_ID": "me@example.com", "APPLE_PASSWORD": "pw", "APPLE_TEAM_ID": "TEAM"}
        self.assertEqual(
            macos_notarize.notarytool_credentials(env),
            ["--apple-id", "me@example.com", "--password", "pw", "--team-id", "TEAM"],
        )

    def test_incomplete_credentials_are_none(self) -> None:
        self.assertIsNone(macos_notarize.notarytool_credentials({"APPLE_API_KEY": "KEY123"}))


class NotarizeDmgTests(unittest.TestCase):
    def test_submits_then_staples_each_dmg(self) -> None:
        with mock.patch.object(macos_notarize, "run") as run:
            macos_notarize.notarize_dmgs([Path("/out/App_1.0_aarch64.dmg")], API_KEY_ENV)
        self.assertEqual(
            [call.args[0][:3] for call in run.call_args_list],
            [["xcrun", "notarytool", "submit"], ["xcrun", "stapler", "staple"]],
        )
        self.assertIn("--wait", run.call_args_list[0].args[0])

    def test_failed_submission_error_omits_the_credentials(self) -> None:
        env = {"APPLE_ID": "me@example.com", "APPLE_PASSWORD": "hunter2", "APPLE_TEAM_ID": "TEAM"}
        failure = subprocess.CalledProcessError(69, ["xcrun", "notarytool", "submit", "--password", "hunter2"])
        with mock.patch.object(macos_notarize, "run", side_effect=failure):
            with self.assertRaises(SystemExit) as raised:
                macos_notarize.notarize_dmgs([Path("/out/App.dmg")], env)
        self.assertNotIn("hunter2", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_retries_stapling_after_a_network_failure(self) -> None:
        failure = subprocess.CalledProcessError(68, ["xcrun", "stapler", "staple"])
        sleeps: list[float] = []
        with mock.patch.object(macos_notarize, "run", side_effect=[failure, None]) as run:
            macos_notarize.staple(Path("/out/App.dmg"), sleep=sleeps.append)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sleeps, [macos_notarize.STAPLE_RETRY_SECONDS])

    def test_gives_up_stapling_after_the_last_attempt(self) -> None:
        failure = subprocess.CalledProcessError(68, ["xcrun", "stapler", "staple"])
        with mock.patch.object(macos_notarize, "run", side_effect=failure) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                macos_notarize.staple(Path("/out/App.dmg"), sleep=lambda _: None)
        self.assertEqual(run.call_count, macos_notarize.STAPLE_ATTEMPTS)

    def test_skips_without_credentials_unless_required(self) -> None:
        with mock.patch.object(macos_notarize, "run") as run:
            macos_notarize.notarize_dmgs([Path("/out/App.dmg")], {})
        run.assert_not_called()
        with self.assertRaises(SystemExit):
            macos_notarize.notarize_dmgs([Path("/out/App.dmg")], {"PACKAGE_REQUIRE_MACOS_SIGNING": "1"})


if __name__ == "__main__":
    unittest.main()
