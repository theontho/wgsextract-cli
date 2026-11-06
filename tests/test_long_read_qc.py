"""#338: `info` reports long reads' N50, length histogram and mapped fraction,
and warns when the N50 is short or too few reads map."""

import random
import subprocess

import pytest

from tests.htslib_helpers import needs_htslib, sam_to_bam, write_fasta
from wgsextract_cli import api
from wgsextract_cli.core.dependencies import get_tool_path

LENGTH = 300_000
QUAL = "5"


@pytest.fixture(scope="module")
def genome(tmp_path_factory):
    seq = "".join(random.Random(338).choices("ACGT", k=LENGTH))
    root = tmp_path_factory.mktemp("lrqc")
    return seq, str(write_fasta(root / "ref.fa", {"chr1": seq}))


def _ont_bam(path, genome, *, read_bp=4_000, unmapped=10, aligned=True):
    """90 reads, one record every 800 bp: each read's primary record and three
    supplementary ones align 1 kb of it (360 mapped records), plus `unmapped`
    reads. So 90 of 100 reads map, though 360 of 370 records do."""
    seq, _ref = genome
    rng = random.Random(1)
    clip = read_bp - 1_000
    records = []
    for k in range(360 if aligned else 0):
        pos = 1 + k * 800
        read = seq[pos - 1 : pos + 999] + "".join(rng.choices("ACGT", k=clip))
        flag = 0 if k % 4 == 0 else 2048
        records.append(
            f"r{k // 4}\t{flag}\tchr1\t{pos}\t60\t1000M{clip}S\t*\t0\t0\t"
            f"{read}\t{QUAL * read_bp}\tRG:Z:ont"
        )
    for i in range(unmapped if aligned else 100):
        read = "".join(rng.choices("ACGT", k=read_bp))
        records.append(
            f"u{i}\t4\t*\t0\t0\t*\t*\t0\t0\t{read}\t{QUAL * read_bp}\tRG:Z:ont"
        )
    header = [
        "@HD\tVN:1.6\tSO:coordinate" if aligned else "@HD\tVN:1.6",
        f"@SQ\tSN:chr1\tLN:{LENGTH}",
        "@RG\tID:ont\tSM:HG002\tPL:ONT",
    ]
    return sam_to_bam(header + records, path)


def _info(bam, genome, tmp_path, detailed=True):
    return api.info.info(
        api.InfoParams(
            input=bam, ref=genome[1], outdir=tmp_path / "out", detailed=detailed
        )
    )


def _codes(result):
    return {w.code: w.details for w in result.warnings}


@needs_htslib
def test_long_reads_get_an_exact_n50_and_length_histogram(genome, tmp_path):
    result = _info(_ont_bam(tmp_path / "ont.bam", genome), genome, tmp_path, False)
    assert result.metrics["read_n50"] == 4_000
    lengths = result.details["read_lengths"]
    # Every read was sampled, so the N50 is exact, not an estimate.
    assert lengths["exact"] and lengths["sampled_reads"] == 100
    [full] = [row for row in lengths["length_histogram"] if row["reads"]]
    assert (full["min_bp"], full["max_bp"], full["reads_pct"]) == (2_000, 5_000, 100)
    assert "short_read_n50" not in _codes(result)


@needs_htslib
def test_short_long_reads_warn_of_a_short_n50(genome, tmp_path):
    bam = _ont_bam(tmp_path / "short.bam", genome, read_bp=1_500)
    warning = _codes(_info(bam, genome, tmp_path, False))["short_read_n50"]
    assert warning["read_n50"] == 1_500


@needs_htslib
def test_mapped_fraction_counts_reads_not_supplementary_records(genome, tmp_path):
    """90 of 100 reads map. idxstats' records (360 of 370) would say 97%, so
    a contaminated or wrongly referenced sample would pass the 95% check."""
    bam = _ont_bam(tmp_path / "ont.bam", genome)
    for _run in ("measured", "cached"):
        result = _info(bam, genome, tmp_path)
        assert result.metrics["mapped_read_pct"] == pytest.approx(90, abs=1.5)
        warning = _codes(result)["low_mapped_read_fraction"]
        assert warning["mapped_read_pct"] == result.metrics["mapped_read_pct"]


@needs_htslib
def test_well_mapped_long_reads_raise_no_mapping_warning(genome, tmp_path):
    result = _info(_ont_bam(tmp_path / "ok.bam", genome, unmapped=0), genome, tmp_path)
    assert result.metrics["mapped_read_pct"] == pytest.approx(100)
    assert "low_mapped_read_fraction" not in _codes(result)


@needs_htslib
def test_unaligned_long_reads_get_an_n50_but_no_mapped_fraction(genome, tmp_path):
    bam = _ont_bam(tmp_path / "ubam.bam", genome, aligned=False)
    result = _info(bam, genome, tmp_path)
    assert result.metrics["read_n50"] == 4_000
    assert "mapped_read_pct" not in result.metrics
    assert "low_mapped_read_fraction" not in _codes(result)


@needs_htslib
def test_short_reads_get_no_long_read_qc(genome, tmp_path):
    seq, _ref = genome
    records = [
        f"s{i}\t0\tchr1\t{1 + i * 100}\t60\t150M\t*\t0\t0\t"
        f"{seq[i * 100 : i * 100 + 150]}\t{QUAL * 150}\tRG:Z:ill"
        for i in range(500)
    ]
    header = ["@HD\tVN:1.6\tSO:coordinate", f"@SQ\tSN:chr1\tLN:{LENGTH}",
              "@RG\tID:ill\tSM:HG002\tPL:ILLUMINA"]  # fmt: skip
    result = _info(sam_to_bam(header + records, tmp_path / "sr.bam"), genome, tmp_path)
    assert "read_n50" not in result.metrics
    assert "mapped_read_pct" not in result.metrics


@needs_htslib
def test_qc_failed_reads_count_in_the_n50(genome, tmp_path):
    """Review of #461: a library of mostly short QC-failed reads got the
    passing reads' healthy N50, called exact."""
    rng = random.Random(2)
    records = [
        f"{name}{i}\t{flag}\t*\t0\t0\t*\t*\t0\t0\t"
        f"{''.join(rng.choices('ACGT', k=n))}\t{QUAL * n}\tRG:Z:ont"
        for name, flag, n, count in (("ok", 4, 4_000, 10), ("qcf", 4 | 512, 1_000, 100))
        for i in range(count)
    ]
    header = ["@HD\tVN:1.6", f"@SQ\tSN:chr1\tLN:{LENGTH}",
              "@RG\tID:ont\tSM:HG002\tPL:ONT"]  # fmt: skip
    bam = sam_to_bam(header + records, tmp_path / "qcfail.bam")
    result = _info(bam, genome, tmp_path, False)
    assert result.metrics["read_n50"] == 1_000
    assert _codes(result)["short_read_n50"]["exact"] is True


@needs_htslib
def test_a_short_ont_library_still_gets_long_read_qc(genome, tmp_path):
    """Review of #461: PL:ONT reads averaging 300 bp fell under the 410 bp
    long-read cut-off, so a failed library got no N50 or warning."""
    bam = _ont_bam(tmp_path / "tiny.bam", genome, read_bp=300, aligned=False)
    result = _info(bam, genome, tmp_path, False)
    assert result.metrics["read_n50"] == 300
    assert "short_read_n50" in _codes(result)


@needs_htslib
def test_sampled_primary_share_counts_records_spanning_several_windows(tmp_path):
    """Review of #461: a primary record that starts in one window and runs
    over the next window's first base was dropped, so long primaries were
    undercounted. Here each read is a 15 kb primary record and a 1 kb
    supplementary one, and a 300 kb contig is sampled by adjacent windows."""
    from wgsextract_cli.commands.info.metrics import parse_idxstats
    from wgsextract_cli.commands.info.render import sampled_primary_share

    records = [
        f"r{i}\t{flag}\tchr1\t{250 + i * 1_000 + offset}\t60\t{cigar}\t*\t0\t0\t*\t*"
        for i in range(280)
        for flag, offset, cigar in ((0, 0, "15000M"), (2048, 500, "1000M14000H"))
    ]
    header = ["@HD\tVN:1.6\tSO:coordinate", f"@SQ\tSN:chr1\tLN:{LENGTH}"]
    bam = str(sam_to_bam(header + records, tmp_path / "span.bam"))
    sampled = sampled_primary_share(bam, None, parse_idxstats(bam)[0], str(tmp_path))
    assert sampled is not None
    assert sampled[0] == pytest.approx(0.5, abs=0.02)


@needs_htslib
def test_sampled_primary_share_does_not_overweight_chrm(tmp_path):
    """Review of #461: chrM's one window samples most of it, a chromosome's a
    few percent, yet their read starts were pooled unweighted. Here chr1's
    reads are each a primary and a supplementary record, chrM's 190 reads
    one primary each: 470 of 750 records (63%) are primary, though chrM
    holds most of the sampled starts."""
    from wgsextract_cli.commands.info.metrics import parse_idxstats
    from wgsextract_cli.commands.info.render import sampled_primary_share

    records = [
        f"r{i}\t{flag}\tchr1\t{250 + i * 1_000 + offset}\t60\t{cigar}\t*\t0\t0\t*\t*"
        for i in range(280)
        for flag, offset, cigar in ((0, 0, "15000M"), (2048, 500, "1000M14000H"))
    ] + [f"m{i}\t0\tchrM\t{7 + i * 80}\t60\t500M\t*\t0\t0\t*\t*" for i in range(190)]
    header = ["@HD\tVN:1.6\tSO:coordinate", f"@SQ\tSN:chr1\tLN:{LENGTH}",
              "@SQ\tSN:chrM\tLN:16569"]  # fmt: skip
    bam = str(sam_to_bam(header + records, tmp_path / "chrm.bam"))
    sampled = sampled_primary_share(bam, None, parse_idxstats(bam)[0], str(tmp_path))
    assert sampled is not None
    assert sampled[0] == pytest.approx(470 / 750, abs=0.03)


@needs_htslib
def test_a_cram_gets_the_same_exact_mapped_fraction_as_its_bam(genome, tmp_path):
    """Review of #461: flagstat rejected --reference, so a small CRAM got no
    mapped fraction and redid the detailed analysis on every run."""
    bam = _ont_bam(tmp_path / "ont.bam", genome)
    cram = tmp_path / "ont.cram"
    samtools = get_tool_path("samtools")
    subprocess.run(
        [samtools, "view", "-C", "-T", genome[1], "-o", str(cram), bam], check=True
    )
    subprocess.run([samtools, "index", str(cram)], check=True)
    expected = _info(bam, genome, tmp_path)
    for _run in ("measured", "cached"):
        result = _info(cram, genome, tmp_path / "cram")
        assert result.metrics["mapped_read_pct"] == expected.metrics["mapped_read_pct"]
        assert "mapped_read_pct" in _codes(result)["low_mapped_read_fraction"]
