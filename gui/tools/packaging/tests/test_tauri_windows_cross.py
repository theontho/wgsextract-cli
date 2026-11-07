from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "posix"))
import package_tauri_windows_cross as cross  # noqa: E402


class TestToolDirs(unittest.TestCase):
    def test_cargo_bin_off_path_is_found_once(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            cargo_bin = Path(home) / ".cargo" / "bin"
            cargo_bin.mkdir(parents=True)
            with (
                mock.patch.dict(os.environ, {"PATH": "/usr/bin"}),
                mock.patch.object(cross.Path, "home", return_value=Path(home)),
                mock.patch.object(cross.shutil, "which", return_value=None),
            ):
                cross._add_tool_dirs_to_path()
                cross._add_tool_dirs_to_path()
                self.assertEqual(os.environ["PATH"], os.pathsep.join(["/usr/bin", str(cargo_bin)]))


if __name__ == "__main__":
    unittest.main()
