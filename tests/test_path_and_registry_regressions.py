import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core.dependencies import get_jar_dir
from wgsextract_cli.core.process_registry import proc_registry
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.variant_files import popen, verify_paths_exist


class TestPathNormalizationAndProcessRegistry(unittest.TestCase):
    def tearDown(self):
        proc_registry.processes.clear()

    def test_verify_paths_exist_expands_tilde_paths(self):
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as tmp_dir:
            target = Path(tmp_dir) / "sample.fa"
            target.write_text(">chr1\nACGT\n", encoding="utf-8")

            tilde_path = str(target).replace(str(Path.home()), "~", 1)

            self.assertTrue(verify_paths_exist({"--input": tilde_path}))

    def test_reference_library_expands_tilde_root(self):
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as tmp_dir:
            ref_dir = Path(tmp_dir)
            fasta_path = ref_dir / "hg38.fa"
            fasta_path.write_text(">chr1\nACGT\n", encoding="utf-8")

            tilde_root = str(ref_dir).replace(str(Path.home()), "~", 1)

            lib = ReferenceLibrary(tilde_root)

            self.assertEqual(lib.fasta, str(fasta_path))

    def test_get_jar_dir_expands_configured_tilde_path(self):
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as tmp_dir:
            tilde_path = str(Path(tmp_dir)).replace(str(Path.home()), "~", 1)

            with patch(
                "wgsextract_cli.core.config.settings", {"jar_directory": tilde_path}
            ):
                self.assertEqual(get_jar_dir(), tmp_dir)

    def test_popen_unregisters_finished_processes_for_same_command(self):
        cmd = [sys.executable, "-c", "import time; time.sleep(0.1)"]
        cmd_key = " ".join(cmd)

        proc1 = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc2 = popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.assertEqual(len(proc_registry.processes[cmd_key]), 2)

        proc1.wait()
        proc2.wait()
        time.sleep(0.2)

        self.assertNotIn(cmd_key, proc_registry.processes)


if __name__ == "__main__":
    unittest.main()
