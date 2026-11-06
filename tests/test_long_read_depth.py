"""Long reads' depth comes from their aligned bases, not records x read length.

#427: on ONT HG002, `info --detailed` reported chrY at 177x where samtools
coverage measured 28x. Each supplementary record carries the whole read,
soft-clipped, but aligns only part of it, and repetitive contigs hold many.
"""

import csv
import io
import json
import logging
import os
import random
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli.core.genotype_qc import AUTOSOMES, CHR_Y, warn_low_depth
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.main import main

LENGTH = 200_000
READ = 2_000
# chr1: whole 2 kb reads every 100 bp, so 20x. chrY: a record every 125 bp
# aligning 250 bp of its 2 kb read (three in four supplementary), so 2x,
# though its records x read length make it 16x.
CHR1_STEP, CHRY_STEP, CHRY_ALIGNED = 100, 125, 250
# An unplaced contig with chrY's pattern, so also 2x.
UNPLACED, UNPLACED_LENGTH = "chrUn_KI270742v1", 50_000


@pytest.fixture(scope="module")
def long_reads(tmp_path_factory):
    root = tmp_path_factory.mktemp("longreads")
    rng = random.Random(427)
    lengths = {"chr1": LENGTH, "chrY": LENGTH, UNPLACED: UNPLACED_LENGTH}
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(n)) for c, n in lengths.items()
    }
    ref = write_fasta(root / "ref.fa", seqs)
    qual = "5" * READ
    records = []
    for i, pos in enumerate(range(1, LENGTH - READ + 2, CHR1_STEP)):
        seq = seqs["chr1"][pos - 1 : pos - 1 + READ]
        records.append(
            f"a{i}\t0\tchr1\t{pos}\t60\t{READ}M\t*\t0\t0\t{seq}\t{qual}\tRG:Z:ont"
        )
    clip = READ - CHRY_ALIGNED
    for contig in ("chrY", UNPLACED):
        last = lengths[contig] - CHRY_ALIGNED + 2
        for i, pos in enumerate(range(1, last, CHRY_STEP)):
            aligned = seqs[contig][pos - 1 : pos - 1 + CHRY_ALIGNED]
            seq = aligned + "".join(rng.choice("ACGT") for _ in range(clip))
            flag = 0 if i % 4 == 0 else 2048
            records.append(
                f"{contig}.{i // 4}\t{flag}\t{contig}\t{pos}\t60\t"
                f"{CHRY_ALIGNED}M{clip}S\t*\t0\t0\t{seq}\t{qual}\tRG:Z:ont"
            )
    header = [
        "@HD\tVN:1.6\tSO:coordinate",
        *(f"@SQ\tSN:{c}\tLN:{n}" for c, n in lengths.items()),
        "@RG\tID:ont\tSM:HG002\tPL:ONT",
    ]
    bam = sam_to_bam(header + records, root / "ont.bam")
    return {"root": root, "ref": str(ref), "bam": bam}


def _info_table(alignment: str, ref: str, outdir) -> dict[str, dict[str, str]]:
    out = io.StringIO()
    argv = ["wgsextract-cli", "info", "--detailed", "--csv", "--input", alignment]
    argv += ["--ref", ref, "--outdir", str(outdir)]
    with redirect_stdout(out), patch.object(sys, "argv", argv):
        main()
    rows = csv.DictReader(io.StringIO(out.getvalue()))
    return {row["Seq Name"]: row for row in rows}


@needs_htslib
def test_info_reports_long_reads_aligned_depth_not_record_count(long_reads, tmp_path):
    table = _info_table(long_reads["bam"], long_reads["ref"], tmp_path)
    assert float(table["chrY"]["Map ARD"]) == pytest.approx(2, abs=0.5)
    assert float(table["chr1"]["Map ARD"]) == pytest.approx(20, rel=0.05)
    # The cached table is what the callers' low-coverage check reads.
    cache = json.loads((tmp_path / "ont.bam.wgse_info.json").read_text())
    assert cache["depth_method"].startswith("depth of sampled")


@needs_htslib
def test_unplaced_contigs_are_sampled_too(long_reads, tmp_path):
    # #440 review: unplaced contigs kept records x read length in Other.
    table = _info_table(long_reads["bam"], long_reads["ref"], tmp_path)
    assert float(table["Other"]["Map ARD"]) == pytest.approx(2, abs=0.5)


@needs_htslib
def test_depth_unsampled_without_an_index_is_measured_once_indexed(
    long_reads, tmp_path
):
    # #440 review: an unindexed BAM's inflated chrY depth was cached as
    # current, so adding the index didn't correct it.
    bam = tmp_path / "noindex.bam"
    bam.write_bytes(open(long_reads["bam"], "rb").read())
    table = _info_table(str(bam), long_reads["ref"], tmp_path)
    assert float(table["chrY"]["Map ARD"]) > 10
    run_command(["samtools", "index", str(bam)])
    table = _info_table(str(bam), long_reads["ref"], tmp_path)
    assert float(table["chrY"]["Map ARD"]) == pytest.approx(2, abs=0.5)


def _cache_reused(alignment: str, ref: str, outdir) -> bool:
    """Whether `info --detailed` reused the cache an earlier run saved,
    rather than reading the alignment again."""
    cache = outdir / f"{os.path.basename(alignment)}.wgse_info.json"
    # In 2001, and a whole second, which every filesystem's clock can hold.
    old = 1_000_000_000 * 10**9
    os.utime(cache, ns=(old, old))
    _info_table(alignment, ref, outdir)
    return cache.stat().st_mtime_ns == old


@needs_htslib
def test_unaligned_long_reads_detailed_info_is_reused(tmp_path):
    # #440 review: a basecaller's unaligned BAM has no contigs to sample, so
    # its depth was never cached and every `info --detailed` reread the file.
    rng = random.Random(1)
    qual = "5" * READ
    records = [
        f"r{i}\t4\t*\t0\t0\t*\t*\t0\t0\t"
        f"{''.join(rng.choice('ACGT') for _ in range(READ))}\t{qual}\tRG:Z:ont"
        for i in range(50)
    ]
    sam = tmp_path / "unaligned.sam"
    sam.write_text("\n".join(["@HD\tVN:1.6", "@RG\tID:ont\tSM:HG002\tPL:ONT"]
                             + records) + "\n")  # fmt: skip
    bam = str(tmp_path / "unaligned.bam")
    run_command(["samtools", "view", "-b", "-o", bam, str(sam)])
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 1000})
    _info_table(bam, str(ref), tmp_path)
    assert _cache_reused(bam, str(ref), tmp_path)


@needs_htslib
def test_unindexed_long_reads_detailed_info_is_reused_until_indexed(
    long_reads, tmp_path
):
    # #440 review: without an index the depth can't be sampled, so it was never
    # cached and every `info --detailed` reread the whole file for idxstats.
    bam = tmp_path / "noindex.bam"
    bam.write_bytes(open(long_reads["bam"], "rb").read())
    _info_table(str(bam), long_reads["ref"], tmp_path)
    assert _cache_reused(str(bam), long_reads["ref"], tmp_path)
    run_command(["samtools", "index", str(bam)])
    assert not _cache_reused(str(bam), long_reads["ref"], tmp_path)


@needs_htslib
@pytest.mark.parametrize("fmt", ["bam", "cram"])
def test_low_coverage_check_catches_shallow_long_read_chrY(
    long_reads, tmp_path, caplog, fmt
):
    alignment = long_reads["bam"]
    if fmt == "cram":
        alignment = str(tmp_path / "ont.cram")
        run_command(
            ["samtools", "view", "-C", "-T", long_reads["ref"], "-o", alignment,
             long_reads["bam"]]
        )  # fmt: skip
        run_command(["samtools", "index", alignment])
    caplog.set_level(logging.INFO)
    depths = warn_low_depth(
        alignment, {AUTOSOMES, CHR_Y}, outdir=str(tmp_path), ref=long_reads["ref"]
    )
    assert depths is not None
    assert depths[CHR_Y] == pytest.approx(2, abs=0.5)
    assert depths[AUTOSOMES] == pytest.approx(20, rel=0.05)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage().split(" depth")[0].split()[-1] for r in warnings] == [CHR_Y]


@needs_htslib
def test_multi_reference_cram_samples_only_the_contigs_its_index_lists(tmp_path):
    # #339: a merged ONT chr20 CRAM, written in multi-reference containers,
    # was sampled across every autosome, leaving chr20 about 20 of the 1,000
    # windows; its combined depth came out 45x where its two runs summed 51x.
    # Here chr2's reads cover the middle half of every 20 kb, so its few
    # windows on a mostly unread genome land where coverage is twice the mean.
    rng = random.Random(339)
    lengths = {"chr1": 20_000_000, "chr2": LENGTH}
    seqs = {c: "".join(rng.choices("ACGT", k=n)) for c, n in lengths.items()}
    ref = str(write_fasta(tmp_path / "ref.fa", seqs))
    starts = [
        block + offset
        for block in range(0, LENGTH, 20_000)
        for offset in range(5_000, 13_001, 50)
    ]
    records = [
        f"a{i}\t0\tchr2\t{pos + 1}\t60\t{READ}M\t*\t0\t0\t"
        f"{seqs['chr2'][pos : pos + READ]}\t{'5' * READ}"
        for i, pos in enumerate(starts)
    ]
    header = ["@HD\tVN:1.6\tSO:coordinate"]
    header += [f"@SQ\tSN:{c}\tLN:{n}" for c, n in lengths.items()]
    bam = sam_to_bam(header + records, tmp_path / "chr2.bam")
    cram = str(tmp_path / "chr2.cram")
    run_command(
        ["samtools", "view", "-C", "-T", ref, "-o", cram,
         "--output-fmt-option", "multi_seq_per_slice=1", bam]
    )  # fmt: skip
    run_command(["samtools", "index", cram])
    depths = warn_low_depth(cram, {AUTOSOMES}, outdir=str(tmp_path), ref=ref)
    assert depths is not None
    assert depths[AUTOSOMES] == pytest.approx(len(starts) * READ / LENGTH, rel=0.05)
