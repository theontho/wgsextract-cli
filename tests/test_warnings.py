import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure src is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from wgsextract_cli.core.warnings import (
    EXPECTED_TIME,
    MESSAGES,
    check_free_space,
    format_time,
    get_free_space_needed,
    indexed_read_count,
    print_warning,
)


class TestWarnings(unittest.TestCase):
    """
    Verification of user communication and safety utilities.

    Goal: Ensure the correctness of time-estimation logic for long tasks, accurate
    formatting of wait times, and precise calculation of disk space requirements
    to prevent process failure due to insufficient storage.
    """

    def test_format_time(self):
        self.assertEqual(format_time(30), "30 seconds")
        self.assertEqual(format_time(120), "2 minutes")
        self.assertEqual(format_time(3600), "1.0 hours")
        self.assertEqual(format_time(7200), "2.0 hours")

    @patch("logging.warning")
    def test_print_warning_expected_time(self, mock_warning):
        # Test a standard action
        print_warning("GenBAMIndex")
        wait_time = EXPECTED_TIME["GenBAMIndex"]
        time_str = format_time(wait_time)
        expected_msg = (
            f"!!! WARNING: {MESSAGES['ExpectedWait'].format(time=time_str)} !!!"
        )
        mock_warning.assert_any_call(expected_msg)

    @patch("logging.warning")
    def test_print_warning_with_threads(self, mock_warning):
        # Test parallelizable action
        action = "ButtonAlignBAM"
        original_time = EXPECTED_TIME[action]

        # 1 thread
        print_warning(action, threads=1)
        time_str_1 = format_time(original_time)
        mock_warning.assert_any_call(
            f"!!! WARNING: {MESSAGES['ExpectedWait'].format(time=time_str_1)} !!!"
        )

        # 8 threads
        mock_warning.reset_mock()
        print_warning(action, threads=8)
        # 8 ** 0.7 = 4.28
        adjusted_time = int(original_time / (8**0.7))
        time_str_8 = format_time(adjusted_time)
        mock_warning.assert_any_call(
            f"!!! WARNING: {MESSAGES['ExpectedWait'].format(time=time_str_8)} !!!"
        )

    @patch("logging.warning")
    def test_print_warning_free_space(self, mock_warning):
        # Manual values
        print_warning("infoFreeSpace", app_name="Sort", size_gb=100, final_gb=50)
        expected_msg = f"!!! {MESSAGES['infoFreeSpace'].format(app='Sort', size=100, final=50)} !!!"
        mock_warning.assert_any_call(expected_msg)

        # Dynamic calculation (60GB file)
        mock_warning.reset_mock()
        file_size = 60 * 1024**3
        temp_needed, final_needed = get_free_space_needed(file_size, "Coord", False)
        # isize_gb = 60; temp_needed = 60*1 + 60 = 120; final_needed = 60
        self.assertEqual(temp_needed, 120)
        self.assertEqual(final_needed, 60)

        print_warning(
            "infoFreeSpace", app_name="Coord Sort", file_size=file_size, is_cram=False
        )
        expected_msg_dynamic = f"!!! {MESSAGES['infoFreeSpace'].format(app='Coord Sort', size=120, final=60)} !!!"
        mock_warning.assert_any_call(expected_msg_dynamic)

    def test_read_count_raises_the_estimate_for_compact_crams(self):
        # The 30x MGI CRAM the per-read constants were measured on: 7.9 GB,
        # 680M reads, which needed ~50 GB of temp files and ~46 GB of FASTQ.
        size, reads = 7_859_324_055, 681_927_272
        self.assertEqual(get_free_space_needed(size, "Name", True), (34, 14))
        self.assertEqual(get_free_space_needed(size, "Name", True, reads), (50, 47))
        self.assertEqual(get_free_space_needed(size, "Coord", True, reads), (50, 14))
        # A BAM whose size already exceeds the per-read figures keeps its estimate.
        self.assertEqual(
            get_free_space_needed(60 * 1024**3, "Coord", False, 10_000_000), (120, 60)
        )

    @patch("wgsextract_cli.core.warnings.run_command")
    def test_indexed_read_count(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "chr1\t248956422\t100\t2\n*\t0\t0\t5\n"
        with tempfile.TemporaryDirectory() as d:
            cram = os.path.join(d, "x.cram")
            open(cram + ".crai", "w").close()
            self.assertEqual(indexed_read_count(cram, 4), 107)
            # Without an index idxstats would decode the whole file, so skip it.
            mock_run.reset_mock()
            self.assertIsNone(indexed_read_count(os.path.join(d, "unindexed.bam")))
            mock_run.assert_not_called()

    @patch("logging.warning")
    def test_print_warning_realign(self, mock_warning):
        print_warning("RealignBAMTimeWarnMesg", threads=4)
        cpus = 4
        estimated_hours = 5 + 160 / cpus
        expected_msg = f"!!! {MESSAGES['RealignBAMTimeWarnMesg'].format(time=f'{estimated_hours:.1f}')} !!!"
        mock_warning.assert_any_call(expected_msg)

    @patch("logging.warning")
    def test_new_warnings(self, mock_warning):
        # Yoruba
        print_warning("YorubaWarning")
        mock_warning.assert_any_call(f"!!! {MESSAGES['YorubaWarning']} !!!")

        # Low Coverage
        mock_warning.reset_mock()
        print_warning("LowCoverageWarning")
        mock_warning.assert_any_call(f"!!! {MESSAGES['LowCoverageWarning']} !!!")

        # Long Read
        mock_warning.reset_mock()
        print_warning("LongReadSequenceWarning")
        mock_warning.assert_any_call(f"!!! {MESSAGES['LongReadSequenceWarning']} !!!")

    @patch("shutil.disk_usage")
    @patch("logging.warning")
    def test_check_free_space(self, mock_warning, mock_disk_usage):
        # Mock 10GB free
        mock_disk_usage.return_value = (100 * 1024**3, 90 * 1024**3, 10 * 1024**3)

        # Enough space
        self.assertTrue(check_free_space(".", 5))
        mock_warning.assert_not_called()

        # Not enough space
        self.assertFalse(check_free_space(".", 20))
        mock_warning.assert_called()


if __name__ == "__main__":
    unittest.main()
