"""Long reads' depth comes from their aligned bases, not records x read length.

#427: on ONT HG002, `info --detailed` reported chrY at 177x where samtools
coverage measured 28x. Each supplementary record carries the whole read,
soft-clipped, but aligns only part of it, and repetitive contigs hold many.
"""

import csv
import io
import json
import logging
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


@pytest.fixture(scope="module")
def long_reads(tmp_path_factory):
    root = tmp_path_factory.mktemp("longreads")
    rng = random.Random(427)
    seqs = {
        c: "".join(rng.choice("ACGT") for _ in range(LENGTH)) for c in ("chr1", "chrY")
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
    for i, pos in enumerate(range(1, LENGTH - CHRY_ALIGNED + 2, CHRY_STEP)):
        aligned = seqs["chrY"][pos - 1 : pos - 1 + CHRY_ALIGNED]
        seq = aligned + "".join(rng.choice("ACGT") for _ in range(clip))
        flag = 0 if i % 4 == 0 else 2048
        records.append(
            f"y{i // 4}\t{flag}\tchrY\t{pos}\t60\t{CHRY_ALIGNED}M{clip}S\t*\t0\t0\t"
            f"{seq}\t{qual}\tRG:Z:ont"
        )
    header = [
        "@HD\tVN:1.6\tSO:coordinate",
        f"@SQ\tSN:chr1\tLN:{LENGTH}",
        f"@SQ\tSN:chrY\tLN:{LENGTH}",
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
