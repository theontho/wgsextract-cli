"""Time and disk-space warnings: what the user reads in the log, and the
read counts and space estimates behind them."""

import logging
import re

import pytest

from tests.disk_volumes import Volumes
from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.core.warnings import (
    check_free_space,
    format_time,
    get_free_space_needed,
    indexed_read_count,
    print_warning,
)

GIB = 1024**3


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_format_time():
    assert format_time(30) == "30 seconds"
    assert format_time(120) == "2 minutes"
    assert format_time(3600) == "1.0 hours"
    assert format_time(7200) == "2.0 hours"


@pytest.mark.parametrize(
    ("action", "threads", "wait"),
    [
        ("ButtonAlignBAM", None, "9.0 hours"),
        ("ButtonAlignBAM", "1", "9.0 hours"),
        # 8 threads: 9 hours / 8 ** 0.7.
        ("ButtonAlignBAM", "8", "2.1 hours"),
        # Indexing doesn't run in parallel, so threads don't shorten it.
        ("GenBAMIndex", "8", "30 minutes"),
    ],
)
def test_expected_wait_shrinks_with_threads_only_for_parallel_actions(
    caplog, action, threads, wait
):
    print_warning(action, threads=threads)
    (message,) = _warnings(caplog)
    assert message.endswith(f"Expected Wait is {wait} !!!")


def test_free_space_warning_gives_temporary_and_final_space(caplog):
    print_warning("infoFreeSpace", app_name="Sort", size_gb=100, final_gb=50)
    # A 60 GiB BAM: the sort needs its size again in temp files, plus the output.
    print_warning(
        "infoFreeSpace", app_name="Coord Sort", file_size=60 * GIB, is_cram=False
    )
    sort, coord = _warnings(caplog)
    assert "Sort needs 100 GB" in sort and "50 GB" in sort
    assert "Coord Sort needs 120 GB" in coord and "60 GB" in coord


def test_space_estimates_for_bam_and_compact_cram():
    assert get_free_space_needed(60 * GIB, "Coord", False) == (120, 60)
    # The 30x MGI CRAM the per-read constants were measured on: 7.9 GB,
    # 680M reads, which needed ~50 GB of temp files and ~46 GB of FASTQ.
    size, reads = 7_859_324_055, 681_927_272
    assert get_free_space_needed(size, "Name", True) == (34, 14)
    assert get_free_space_needed(size, "Name", True, reads) == (50, 47)
    assert get_free_space_needed(size, "Coord", True, reads) == (50, 14)
    # A BAM whose size already exceeds the per-read figures keeps its estimate.
    assert get_free_space_needed(60 * GIB, "Coord", False, 10_000_000) == (120, 60)


def test_realign_estimate_scales_with_cpus(caplog):
    print_warning("RealignBAMTimeWarnMesg", threads=4)
    (message,) = _warnings(caplog)
    assert "45.0" in message  # 5 hours + 160 / 4 CPUs
    # No fixed range of hours contradicts the estimate.
    assert not re.search(r"\(\d+(?:-\d+)? hours\)", message)


@pytest.mark.parametrize(
    ("key", "fact"),
    [
        ("YorubaWarning", "Yoruba"),
        ("LowCoverageWarning", "low mapped average read depth"),
        ("LongReadSequenceWarning", "long-read sequencer"),
    ],
)
def test_message_warnings_are_logged(caplog, key, fact):
    print_warning(key)
    (message,) = _warnings(caplog)
    assert fact in message


@needs_tools("samtools")
@pytest.mark.parametrize("fmt", ["bam", "cram"])
def test_indexed_read_count_counts_mapped_and_unmapped_reads(tmp_path, fmt):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 50})
    read = "ACGTACGTAC\tIIIIIIIIII"
    bam = sam_to_bam(
        [
            "@HD\tVN:1.6\tSO:coordinate",
            "@SQ\tSN:chr1\tLN:200",
            *(
                f"m{i}\t0\tchr1\t{1 + 4 * i}\t60\t10M\t*\t0\t0\t{read}"
                for i in range(3)
            ),
            *(f"u{i}\t4\t*\t0\t0\t*\t*\t0\t0\t{read}" for i in range(2)),
        ],
        tmp_path / "reads.bam",
    )
    path = bam
    if fmt == "cram":
        path = str(tmp_path / "reads.cram")
        run_command(["samtools", "view", "-C", "-T", str(ref), "-o", path, bam])
        run_command(["samtools", "index", path])

    assert indexed_read_count(path, 4) == 5


def test_unindexed_alignment_has_no_quick_read_count(tmp_path):
    # Without an index samtools would decode the whole file.
    alignment = tmp_path / "unindexed.bam"
    alignment.write_bytes(b"not decoded")
    assert indexed_read_count(alignment) is None


def test_check_free_space_warns_with_the_shortfall(tmp_path, monkeypatch, caplog):
    Volumes(monkeypatch).free[tmp_path] = 10

    assert check_free_space(tmp_path, 5)
    assert _warnings(caplog) == []
    assert not check_free_space(tmp_path, 20)
    text = "\n".join(_warnings(caplog))
    assert "Insufficient Disk Space" in text
    assert f"10.0 GB available on {tmp_path}" in text
    assert "20 GB needed" in text
