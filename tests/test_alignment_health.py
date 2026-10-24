"""How an alignment was made: duplicates marked, quality binning, the warnings."""

from pathlib import Path

from tests.htslib_helpers import needs_htslib, sam_to_bam
from wgsextract_cli.commands.info.render import render_info
from wgsextract_cli.core import alignment_health as health_module
from wgsextract_cli.core.alignment_health import alignment_health, warn_about_alignment


def _bam(tmp_path: Path, n: int, dup_every: int = 0, length: int = 100) -> str:
    """n reads near chr1:20 Mb; every dup_every-th is flagged a duplicate."""
    seq = "ACGT" * (length // 4)
    quals = "I#5?" * (length // 4)
    reads = [
        f"r{i}\t{1024 if dup_every and i % dup_every == 0 else 0}\tchr1\t"
        f"{20_000_001 + 50 * i}\t60\t{length}M\t*\t0\t0\t{seq}\t{quals}"
        for i in range(n)
    ]
    return sam_to_bam(
        ["@SQ\tSN:chr1\tLN:248956422", *reads], tmp_path / f"s{n}_{dup_every}.bam"
    )


@needs_htslib
def test_marked_duplicates_and_quality_levels_are_read(tmp_path):
    health = alignment_health(_bam(tmp_path, 2500, dup_every=20))
    assert health["duplicates_marked"] is True
    assert health["duplicate_pct"] == 5.0
    assert health["quality_levels"] == 4
    assert health["alt_aware"] is None  # no ALT contigs to judge by


@needs_htslib
def test_unmarked_duplicates_are_recognised_and_warned_about(tmp_path, caplog):
    bam = _bam(tmp_path, 2500)
    assert alignment_health(bam)["duplicates_marked"] is False
    warn_about_alignment(bam)
    assert "never marked" in caplog.text


@needs_htslib
def test_no_verdict_from_too_few_reads_or_long_reads(tmp_path):
    assert alignment_health(_bam(tmp_path, 100))["duplicates_marked"] is None
    # PacBio and ONT reads aren't duplicate-marked as a rule.
    long_reads = _bam(tmp_path, 2500, length=2000)
    assert alignment_health(long_reads)["duplicates_marked"] is None


def test_unindexed_input_gets_no_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(health_module, "alt_unaware", lambda *_a: None)
    bam = tmp_path / "x.bam"
    bam.write_bytes(b"")
    assert alignment_health(bam) == dict.fromkeys(
        ("duplicates_marked", "duplicate_pct", "quality_levels", "alt_aware")
    )


def test_info_shows_how_the_alignment_was_made():
    text = render_info(
        {
            "alignment_health": {
                "duplicates_marked": False,
                "quality_levels": 41,
                "alt_aware": False,
            }
        }
    )
    assert "Duplicates Marked           No" in text
    assert "Base Quality Values         41 (full resolution)" in text
    assert "ALT-aware Mapping           No" in text
    marked = render_info(
        {"alignment_health": {"duplicates_marked": True, "duplicate_pct": 3.3,
                              "quality_levels": 4, "alt_aware": True}}
    )  # fmt: skip
    assert "Yes (3.3% of reads" in marked and "4 (binned)" in marked
