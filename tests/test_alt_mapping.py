"""ALT contigs: BWA alignment made ALT-aware, and ALT-unaware alignments found."""

import random
import subprocess
from pathlib import Path

import pytest

from wgsextract_cli.commands.align import alt_contigs, ensure_bwa_alt_file
from wgsextract_cli.core.alt_mapping import (
    PROBE_ALT_CONTIG,
    alt_unaware,
    warn_if_alt_unaware,
)
from wgsextract_cli.core.dependencies import get_tool_path

needs_samtools = pytest.mark.skipif(
    not get_tool_path("samtools"), reason="samtools missing"
)


def _reference_with_alt(tmp_path: Path) -> tuple[Path, str]:
    """chr1 and an ALT copy of 10 kb of it, differing at 3 positions."""
    rng = random.Random(1)
    chrom = "".join(rng.choice("ACGT") for _ in range(20000))
    alt = list(chrom[5000:15000])
    for pos in (2000, 5000, 8000):
        alt[pos] = "C" if alt[pos] == "A" else "A"
    ref = tmp_path / "ref.fa"
    ref.write_text(
        f">chr1\n{chrom}\n>chr1_KI000001v1_alt\n{''.join(alt)}\n"
        f">HLA-A*01:01:01:01\n{chrom[:500]}\n>chrUn_JTFH01000001v1_decoy\nACGT\n"
    )
    subprocess.run([get_tool_path("samtools"), "faidx", str(ref)], check=True)
    return ref, chrom


@needs_samtools
def test_the_alt_file_lists_alt_and_hla_contigs(tmp_path):
    ref, _ = _reference_with_alt(tmp_path)
    assert alt_contigs(str(ref)) == ["chr1_KI000001v1_alt", "HLA-A*01:01:01:01"]
    ensure_bwa_alt_file(str(ref))
    assert Path(f"{ref}.alt").read_text() == "chr1_KI000001v1_alt\nHLA-A*01:01:01:01\n"


@needs_samtools
def test_an_existing_alt_file_is_kept_and_none_is_made_without_alts(tmp_path):
    ref, _ = _reference_with_alt(tmp_path)
    Path(f"{ref}.alt").write_text("@SQ\tSN:chr1\tLN:20000\nbwakit\n")
    ensure_bwa_alt_file(str(ref))
    assert Path(f"{ref}.alt").read_text().endswith("bwakit\n")

    plain = tmp_path / "plain.fa"
    plain.write_text(">chr1\nACGTACGT\n")
    ensure_bwa_alt_file(str(plain))
    assert not Path(f"{plain}.alt").exists()


@pytest.mark.skipif(
    not (get_tool_path("bwa") and get_tool_path("samtools")),
    reason="bwa/samtools missing",
)
def test_bwa_with_the_alt_file_maps_reads_to_the_chromosome(tmp_path):
    ref, chrom = _reference_with_alt(tmp_path)
    bwa = get_tool_path("bwa")
    subprocess.run([bwa, "index", str(ref)], check=True, capture_output=True)
    rng = random.Random(2)
    reads = tmp_path / "reads.fq"
    with open(reads, "w") as f:
        for i in range(100):
            # Between the copies' differences at 7,000 and 10,000: identical in both.
            start = rng.randint(7001, 9899)
            f.write(f"@r{i}\n{chrom[start : start + 100]}\n+\n{'I' * 100}\n")

    def primary_mapq(sam: str) -> list[tuple[str, int]]:
        rows = [line.split("\t") for line in sam.splitlines() if line[0] != "@"]
        return [(r[2], int(r[4])) for r in rows if not int(r[1]) & 0x900]

    def align() -> list[tuple[str, int]]:
        sam = subprocess.run(
            [bwa, "mem", str(ref), str(reads)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return primary_mapq(sam)

    # Without the file a read matches both copies equally: mapping quality 0.
    assert all(mapq == 0 for _, mapq in align())
    ensure_bwa_alt_file(str(ref))
    hits = align()
    assert all(contig == "chr1" and mapq >= 20 for contig, mapq in hits)


def _probe_bam(tmp_path: Path, mapq: int, with_alt: bool = True) -> str:
    header = ["@SQ\tSN:chr1\tLN:3000000"]
    if with_alt:
        header.append(f"@SQ\tSN:{PROBE_ALT_CONTIG}\tLN:354444")
    reads = [
        f"r{i}\t0\tchr1\t{2470000 + 40 * i}\t{mapq}\t100M\t*\t0\t0\t{'A' * 100}\t"
        f"{'I' * 100}"
        for i in range(250)
    ]
    sam = tmp_path / f"probe{mapq}{with_alt}.sam"
    sam.write_text("\n".join(header + reads) + "\n")
    bam = str(sam.with_suffix(".bam"))
    samtools = get_tool_path("samtools")
    subprocess.run([samtools, "sort", "-o", bam, str(sam)], check=True)
    subprocess.run([samtools, "index", bam], check=True)
    return bam


@needs_samtools
def test_alt_unaware_alignments_are_recognised(tmp_path, caplog):
    unaware = _probe_bam(tmp_path, mapq=0)
    assert alt_unaware(unaware) is True
    assert alt_unaware(_probe_bam(tmp_path, mapq=60)) is False
    # No ALT contigs (an analysis set, or GRCh37): nothing to tell.
    assert alt_unaware(_probe_bam(tmp_path, mapq=0, with_alt=False)) is None
    warn_if_alt_unaware(unaware)
    assert "without ALT-aware mapping" in caplog.text
