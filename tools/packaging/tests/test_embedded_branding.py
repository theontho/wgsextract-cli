from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.packaging.embedded_branding import (
    app_name_without_distribution_suffix,
    load_embedded_branding,
)


class EmbeddedBrandingTests(unittest.TestCase):
    def test_effective_app_name_strips_distribution_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name)
            bundle = repo_root / "examples/WGSExtract"
            bundle.mkdir(parents=True)
            (bundle / "manifest.json").write_text(
                json.dumps({"id": "wgs-extract", "version": "0.1.4"}),
                encoding="utf-8",
            )
            env = {
                "EMBEDDED_APP_NAME": "",
                "EMBEDDED_BUNDLE_PATH": "",
                "PACKAGE_BUNDLE_PATH": "examples/WGSExtract",
                "PACKAGE_APP_NAME": "WGSExtract macOS",
            }

            with patch.dict(os.environ, env, clear=False):
                branding = load_embedded_branding(repo_root)

            self.assertEqual(branding.effective_app_name, "WGSExtract")
            self.assertEqual(branding.effective_app_version, "0.1.4")

    def test_app_name_without_distribution_suffix_strips_known_suffixes(self) -> None:
        self.assertEqual(
            app_name_without_distribution_suffix("WGSExtract macOS WebUI"),
            "WGSExtract",
        )
        self.assertEqual(
            app_name_without_distribution_suffix("WGSExtract Linux AppImage WebUI"),
            "WGSExtract",
        )
        self.assertEqual(
            app_name_without_distribution_suffix("WGSExtract"),
            "WGSExtract",
        )

    def test_load_embedded_branding_accepts_manifest_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            repo_root = Path(tmp_dir_name)
            bundle = repo_root / "examples/WGSExtract"
            bundle.mkdir(parents=True)
            (bundle / "manifest.json").write_text(
                """
                {
                  "id": "wgs-extract",
                  // Keep bundle and tool versions in sync.
                  "version": "0.6.5",
                  "summary": "https://example.com/not-a-comment"
                }
                """,
                encoding="utf-8",
            )
            env = {
                "EMBEDDED_APP_NAME": "",
                "EMBEDDED_BUNDLE_PATH": "",
                "PACKAGE_BUNDLE_PATH": "examples/WGSExtract",
                "PACKAGE_APP_NAME": "",
                "PACKAGE_APP_VERSION": "",
                "EMBEDDED_APP_VERSION": "",
            }

            with (
                patch.dict(os.environ, env, clear=False),
                patch("tools.packaging.embedded_branding.get_path", return_value=""),
            ):
                branding = load_embedded_branding(repo_root)

            self.assertEqual(branding.effective_app_version, "0.6.5")


if __name__ == "__main__":
    unittest.main()
