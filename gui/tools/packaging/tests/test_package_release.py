from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "posix"))
import package_release  # noqa: E402


class MacOSUpdaterArchiveTests(unittest.TestCase):
    def test_arch_maps_apple_silicon_and_intel(self) -> None:
        self.assertEqual(package_release.macos_updater_arch("arm64"), "aarch64")
        self.assertEqual(package_release.macos_updater_arch("x86_64"), "x64")

    def test_archives_and_signatures_gain_arch_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            bundle_dir = Path(tmp_dir_name) / "bundle"
            dest = Path(tmp_dir_name) / "dest"
            (bundle_dir / "macos").mkdir(parents=True)
            dest.mkdir()
            (bundle_dir / "macos" / "WGSExtract.app.tar.gz").write_text("archive\n", encoding="utf-8")
            (bundle_dir / "macos" / "WGSExtract.app.tar.gz.sig").write_text("sig\n", encoding="utf-8")

            package_release.stage_macos_updater_archives(bundle_dir, dest, "aarch64")

            self.assertEqual(
                sorted(path.name for path in dest.iterdir()),
                ["WGSExtract_aarch64.app.tar.gz", "WGSExtract_aarch64.app.tar.gz.sig"],
            )


if __name__ == "__main__":
    unittest.main()
