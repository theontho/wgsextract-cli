from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.packaging import generate_update_feeds


class GenerateUpdateFeedsTests(unittest.TestCase):
    def test_tauri_feed_uses_github_release_asset_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            artifact = tmp_dir / "WGSExtract_0.1.4_x64-setup.exe"
            artifact.write_text("archive\n", encoding="utf-8")
            (tmp_dir / "WGSExtract_0.1.4_x64-setup.exe.sig").write_text(
                "signature\n",
                encoding="utf-8",
            )
            output_path = tmp_dir / "latest.json"

            wrote = generate_update_feeds.write_tauri_latest_json(
                output_path,
                generate_update_feeds.collect_artifacts(tmp_dir),
                "0.1.4",
                "https://github.com/WGSExtract/gui-for-cli/releases/download/v0.1.4",
            )

            self.assertTrue(wrote)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["platforms"]["windows-x86_64"]["url"],
                "https://github.com/WGSExtract/gui-for-cli/releases/download/v0.1.4/WGSExtract_0.1.4_x64-setup.exe",
            )
            self.assertEqual(payload["platforms"]["windows-x86_64"]["signature"], "signature")

    def test_tauri_feed_includes_macos_archives_by_arch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            for arch in ("x64", "aarch64"):
                artifact = tmp_dir / f"WGSExtract_{arch}.app.tar.gz"
                artifact.write_text("archive\n", encoding="utf-8")
                (tmp_dir / f"WGSExtract_{arch}.app.tar.gz.sig").write_text(
                    f"{arch}-signature\n",
                    encoding="utf-8",
                )
            output_path = tmp_dir / "latest.json"

            wrote = generate_update_feeds.write_tauri_latest_json(
                output_path,
                generate_update_feeds.collect_artifacts(tmp_dir),
                "0.1.4",
                "https://github.com/WGSExtract/gui-for-cli/releases/download/v0.1.4",
            )

            self.assertTrue(wrote)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["platforms"]["darwin-aarch64"],
                {
                    "signature": "aarch64-signature",
                    "url": "https://github.com/WGSExtract/gui-for-cli/releases/download/v0.1.4/WGSExtract_aarch64.app.tar.gz",
                },
            )
            self.assertEqual(payload["platforms"]["darwin-x86_64"]["signature"], "x64-signature")

    def test_tauri_feed_skips_unsigned_macos_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            (tmp_dir / "WGSExtract_aarch64.app.tar.gz").write_text("archive\n", encoding="utf-8")
            output_path = tmp_dir / "latest.json"

            wrote = generate_update_feeds.write_tauri_latest_json(
                output_path,
                generate_update_feeds.collect_artifacts(tmp_dir),
                "0.1.4",
                "https://github.com/WGSExtract/gui-for-cli/releases/download/v0.1.4",
            )

            self.assertFalse(wrote)
            self.assertFalse(output_path.exists())

    def test_collect_artifacts_rejects_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            (tmp_dir / "a").mkdir()
            (tmp_dir / "b").mkdir()
            (tmp_dir / "a" / "same.dmg").write_text("a\n", encoding="utf-8")
            (tmp_dir / "b" / "same.dmg").write_text("b\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Duplicate release artifact names"):
                generate_update_feeds.collect_artifacts(tmp_dir)

    def test_collect_artifacts_ignores_unpacked_app_bundle_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            app_contents = tmp_dir / "tauri-macos" / "WGSExtract.app" / "Contents"
            app_contents.mkdir(parents=True)
            (app_contents / "Info.plist").write_text("app\n", encoding="utf-8")
            dmg = tmp_dir / "tauri-macos" / "WGSExtract_0.1.4_aarch64.dmg"
            dmg.write_text("dmg\n", encoding="utf-8")

            artifacts = generate_update_feeds.collect_artifacts(tmp_dir)

            self.assertEqual(artifacts, {"WGSExtract_0.1.4_aarch64.dmg": dmg})

    def test_release_asset_name_replaces_spaces(self) -> None:
        self.assertEqual(
            generate_update_feeds.release_asset_name("WGSExtract_0.1.4_x64-setup.exe.sig"),
            "WGSExtract_0.1.4_x64-setup.exe.sig",
        )


if __name__ == "__main__":
    unittest.main()
