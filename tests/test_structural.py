"""`vcf sv` (Delly, pbsv, Sniffles) and `vcf cnv` (Delly): Delly runs for real
on a tiny genome with a 2 kb deletion; pbsv and Sniffles are stood in for."""

import logging
import random
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.htslib_helpers import (
    needs_htslib,
    needs_tools,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
)
from wgsextract_cli.commands.vcf import structural
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

needs_delly = needs_tools("delly", "bcftools", "bgzip", "tabix", "samtools")

GENOME = 60_000
DELETION = (30_000, 32_000)  # 0-based, half-open: missing from the sample


def _sample(tmp_path: Path, deletion: bool = True) -> tuple[str, str]:
    """(BAM, reference): ~10,000 read pairs over chr1 of a sample missing
    DELETION; the BAM also lists a decoy contig the reference lacks, with one
    read whose mate is there."""
    rng = random.Random(1)
    seq = "".join(rng.choice("ACGT") for _ in range(GENOME))
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": seq})
    start, end = DELETION if deletion else (GENOME, GENOME)
    sample = seq[:start] + seq[end:]

    def ref_pos(pos: int) -> int:  # 1-based reference position of a sample one
        return pos + 1 if pos < start else pos + 1 + end - start

    lines = [
        "@HD\tVN:1.6\tSO:coordinate",
        f"@SQ\tSN:chr1\tLN:{GENOME}",
        "@SQ\tSN:chrUn_decoy\tLN:1000",
        "@RG\tID:1\tSM:S\tPL:ILLUMINA",
    ]
    qual = "I" * 100
    for i, a in enumerate(range(0, len(sample) - 500, 6)):
        b = a + rng.randint(280, 320) - 100
        if a < start < a + 100 or b < start < b + 100:
            continue  # no split reads: the pairs' spans show the deletion
        r1, r2 = ref_pos(a), ref_pos(b)
        tlen = r2 + 100 - r1
        lines += [
            f"p{i}\t99\tchr1\t{r1}\t60\t100M\t=\t{r2}\t{tlen}\t{sample[a : a + 100]}"
            f"\t{qual}\tRG:Z:1",
            f"p{i}\t147\tchr1\t{r2}\t60\t100M\t=\t{r1}\t{-tlen}\t"
            f"{sample[b : b + 100]}\t{qual}\tRG:Z:1",
        ]
    lines.append(
        f"d0\t65\tchr1\t5001\t60\t100M\tchrUn_decoy\t100\t0\t{seq[5000:5100]}\t{qual}"
        "\tRG:Z:1"
    )
    return sam_to_bam(lines, tmp_path / "s.bam"), str(ref)


def _records(vcf: Path, region: str | None = None) -> list[tuple[str, ...]]:
    query = [get_tool_path("bcftools"), "query", "-f", "%CHROM %POS %INFO/END\n"]
    query += ["-r", region] if region else []
    out = subprocess.run(
        [*query, str(vcf)], capture_output=True, text=True, check=True
    ).stdout
    return [tuple(line.split()) for line in out.splitlines()]


def _near_deletion(records: list[tuple[str, ...]]) -> bool:
    return any(
        c == "chr1"
        and abs(int(p) - DELETION[0]) < 300
        and abs(int(e) - DELETION[1]) < 300
        for c, p, e in records
    )


def _run(command: str, bam: str, ref: str, outdir: Path, *extra: str) -> None:
    run_wgsextract(
        "vcf", command, "--input", bam, "--ref", ref, "--outdir", str(outdir),
        "--threads", "1", *extra,
    )  # fmt: skip


# --- Delly ----------------------------------------------------------------------


@needs_delly
@pytest.mark.parametrize("region", [None, "chr1:20000-40000"])
def test_delly_sv_finds_the_deletion(tmp_path, region):
    bam, ref = _sample(tmp_path)
    out = tmp_path / "out"
    _run("sv", bam, ref, out, *(["--region", region] if region else []))
    records = _records(out / "sv.vcf.gz")
    assert len(records) == 1 and _near_deletion(records)
    # The BCF and any region BAM are gone.
    assert sorted(p.name for p in out.iterdir()) == ["sv.vcf.gz", "sv.vcf.gz.tbi"]


@needs_delly
def test_delly_sv_on_a_cram_region(tmp_path):
    bam, ref = _sample(tmp_path)
    cram = str(tmp_path / "s.cram")
    samtools = get_tool_path("samtools")
    run_command([samtools, "view", "-C", "-T", ref, "-o", cram, bam])
    run_command([samtools, "index", cram])
    _run("sv", cram, ref, tmp_path / "out", "--region", "chr1")
    assert _near_deletion(_records(tmp_path / "out" / "sv.vcf.gz"))


@needs_delly
def test_no_svs_warn_only_for_a_whole_genome(tmp_path, caplog):
    bam, ref = _sample(tmp_path, deletion=False)
    with caplog.at_level(logging.WARNING):
        _run("sv", bam, ref, tmp_path / "region", "--region", "chr1:1-20000")
    assert "No variants were called" not in caplog.text
    with caplog.at_level(logging.WARNING):
        _run("sv", bam, ref, tmp_path / "whole")
    assert "No variants were called" in caplog.text
    assert _records(tmp_path / "whole" / "sv.vcf.gz") == []


@needs_delly
def test_region_bam_takes_the_reference_contigs(tmp_path):
    bam, ref = _sample(tmp_path)
    region_bam = str(tmp_path / "region.bam")
    args = structural.argparse.Namespace(input=bam, region="chr1:4000-6000")
    structural._extract_region_bam_with_reference_header(args, ref, region_bam)
    samtools = get_tool_path("samtools")
    header = run_command([samtools, "view", "-H", region_bam], capture_output=True)
    assert [line for line in header.stdout.splitlines() if "@SQ" in line] == [
        f"@SQ\tSN:chr1\tLN:{GENOME}"
    ]
    assert "@RG\tID:1" in header.stdout
    reads = run_command([samtools, "view", region_bam], capture_output=True).stdout
    expected = run_command(
        [samtools, "view", "-c", bam, "chr1:4000-6000"], capture_output=True
    ).stdout
    assert len(reads.splitlines()) == int(expected)
    # The read whose mate is on the dropped decoy is kept, its mate unknown.
    mate = next(r for r in reads.splitlines() if r.startswith("d0\t")).split("\t")
    assert mate[2] == "chr1" and mate[6] == "*"


@needs_delly
def test_region_extraction_and_delly_failures_are_reported(tmp_path):
    bam, ref = _sample(tmp_path)
    # Without its index the BAM's region can't be read.
    unindexed = tmp_path / "unindexed.bam"
    shutil.copy(bam, unindexed)
    with pytest.raises(WGSExtractError, match="SV region extraction failed.*exit 1"):
        _run("sv", str(unindexed), ref, tmp_path / "out", "--region", "chr1")
    with pytest.raises(WGSExtractError, match="CNV region extraction failed"):
        _run("cnv", str(unindexed), ref, tmp_path / "out", "--region", "chr1")
    # Too few reads in the region for Delly to estimate the library.
    with pytest.raises(WGSExtractError, match="SV calling failed with exit code 1"):
        _run("sv", bam, ref, tmp_path / "out", "--region", "chr1:100-300")
    # Too little coverage over the genome for its GC correction.
    with pytest.raises(WGSExtractError, match="CNV calling failed with exit code 1"):
        _run("cnv", bam, ref, tmp_path / "out", "--region", "chr1:1-20000")
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".")]


@needs_delly
def test_rerunning_sv_replaces_the_index(tmp_path):
    bam, ref = _sample(tmp_path)
    out = tmp_path / "out"
    _run("sv", bam, ref, out, "--region", "chr1:1-20000")  # no SVs
    _run("sv", bam, ref, out)
    assert _near_deletion(_records(out / "sv.vcf.gz", region="chr1"))


@needs_delly
def test_delly_cnv_finds_the_deletion_and_takes_a_ploidy(tmp_path, caplog):
    bam, ref = _sample(tmp_path)
    out = tmp_path / "out"
    with (
        caplog.at_level(logging.WARNING),
        patch.object(structural, "run_command", wraps=structural.run_command) as rc,
    ):
        _run("cnv", bam, ref, out, "--ploidy", "2", "--map", "unused.map")
    delly = next(c.args[0] for c in rc.call_args_list if c.args[0][0] == "delly")
    assert delly[:2] == ["delly", "cnv"] and delly[delly.index("-y") + 1] == "2"
    assert "Ignoring --map" in caplog.text
    assert _near_deletion(_records(out / "cnv.vcf.gz"))
    assert sorted(p.name for p in out.iterdir()) == ["cnv.vcf.gz", "cnv.vcf.gz.tbi"]


# --- pbsv and Sniffles (stood in for) -------------------------------------------

SV_VCF = (
    "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=60000>\n"
    '##INFO=<ID=SVTYPE,Number=1,Type=String,Description="t">\n'
    '##INFO=<ID=END,Number=1,Type=Integer,Description="e">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
)


class _LongReadCallers:
    """Stands in for pbsv and Sniffles in structural.run_command, writing the
    VCF each would (one deletion, or none); everything else runs for real."""

    def __init__(self, records: str = "chr1\t30000\tdel\tN\t<DEL>\t60\tPASS\t"):
        self.records = records + "SVTYPE=DEL;END=32000\n" if records else ""
        self.calls: list[list[str]] = []
        self.fail = ""

    def __call__(self, cmd, *args, **kwargs):
        if cmd[0] not in ("pbsv", "sniffles"):
            return run_command(cmd, *args, **kwargs)
        self.calls.append(list(cmd))
        if cmd[0] == self.fail:
            raise subprocess.CalledProcessError(3, cmd)
        if cmd[:2] == ["pbsv", "discover"]:
            Path(cmd[-1]).write_bytes(b"svsig")
        elif cmd[:2] == ["pbsv", "call"]:
            assert Path(cmd[-2]).read_bytes() == b"svsig"
            Path(cmd[-1]).write_text(SV_VCF + self.records)
        else:
            Path(cmd[cmd.index("--vcf") + 1]).write_text(SV_VCF + self.records)


@pytest.fixture
def long_read(tmp_path):
    """(callers, bam, ref) with pbsv and Sniffles 'installed'."""
    callers = _LongReadCallers()
    bam = tmp_path / "hifi.bam"
    bam.write_bytes(b"")
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 15_000})
    with (
        patch.object(structural, "run_command", callers),
        patch.object(structural, "verify_dependencies"),
        patch.object(structural, "log_dependency_info"),
        patch("wgsextract_cli.commands.vcf.basic.warn_about_alignment"),
        patch("wgsextract_cli.commands.vcf.basic.calculate_bam_md5", return_value=""),
        patch.object(structural, "get_tool_path", side_effect=lambda t: t),
    ):
        yield callers, str(bam), str(ref)


@needs_htslib
def test_pacbio_runs_pbsv_discover_then_call(tmp_path, long_read):
    callers, bam, ref = long_read
    repeats = tmp_path / "trf.bed"
    repeats.write_text("chr1\t0\t100\n")
    out = tmp_path / "out"
    _run("sv", bam, ref, out, "--pacbio", "--tandem-repeats", str(repeats))

    discover, call = callers.calls
    assert discover[:2] == ["pbsv", "discover"]
    assert discover[discover.index("--tandem-repeats") + 1] == str(repeats)
    assert "--hifi" in discover and "--region" not in discover
    assert discover[-2] == bam
    assert call[:4] == ["pbsv", "call", "-j", "1"] and "--ccs" in call
    assert call[-3] == ref
    assert _records(out / "pbsv.vcf.gz") == [("chr1", "30000", "32000")]
    assert sorted(p.name for p in out.iterdir()) == ["pbsv.vcf.gz", "pbsv.vcf.gz.tbi"]


@needs_htslib
def test_pbsv_without_pacbio_uses_default_thresholds(tmp_path, long_read):
    callers, bam, ref = long_read
    _run("sv", bam, ref, tmp_path / "out", "--caller", "pbsv")
    discover, call = callers.calls
    assert "--hifi" not in discover and "--ccs" not in call


@needs_htslib
def test_pbsv_failures_are_reported(tmp_path, long_read):
    callers, bam, ref = long_read
    with pytest.raises(WGSExtractError, match="--tandem-repeats"):
        _run("sv", bam, ref, tmp_path / "out", "--caller", "pbsv",
             "--tandem-repeats", str(tmp_path / "missing.bed"))  # fmt: skip
    assert callers.calls == []
    callers.fail = "pbsv"
    with pytest.raises(WGSExtractError, match="PacBio SV calling failed .* 3"):
        _run("sv", bam, ref, tmp_path / "out", "--caller", "pbsv")
    assert not list((tmp_path / "out").iterdir())


@needs_htslib
def test_pacbio_prefers_pbsv_when_installed(tmp_path, long_read):
    callers, bam, ref = long_read
    _run("sv", bam, ref, tmp_path / "out", "--pacbio")
    assert [c[0] for c in callers.calls] == ["pbsv", "pbsv"]
    callers.calls.clear()
    _run("sv", bam, ref, tmp_path / "out", "--pacbio", "--caller", "sniffles")
    assert [c[0] for c in callers.calls] == ["sniffles"]


@needs_htslib
def test_sniffles_whole_genome_and_its_warnings(tmp_path, long_read, caplog):
    callers, bam, ref = long_read
    out = tmp_path / "out"
    _run("sv", bam, ref, out, "--caller", "sniffles")
    (cmd,) = callers.calls
    assert "--regions" not in cmd
    assert cmd[cmd.index("--reference") + 1] == ref
    assert cmd[cmd.index("--threads") + 1] == "1"
    assert _records(out / "sniffles.vcf.gz") == [("chr1", "30000", "32000")]

    callers.records = ""
    with caplog.at_level(logging.WARNING):
        _run("sv", bam, ref, tmp_path / "empty", "--caller", "sniffles")
    assert "No variants were called" in caplog.text

    callers.fail = "sniffles"
    with pytest.raises(WGSExtractError, match="Sniffles SV calling failed"):
        _run("sv", bam, ref, out, "--caller", "sniffles", "--region", "chr1:1-100")
    with pytest.raises(WGSExtractError, match="Invalid region"):
        _run("sv", bam, ref, out, "--caller", "sniffles", "--region", "chr1:9-2")
