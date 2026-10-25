"""repair ftdna-bam / ftdna-vcf on tiny files read back with samtools/bcftools."""

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    run_wgsextract,
    write_fasta,
)
from wgsextract_cli.commands import repair
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = needs_htslib

_GENOME = "ACGTTGCAAC" * 30  # 300 bp
_SEQ, _QUAL = _GENOME[100:120], "ABCDEFGHIJKLMNOPQRST"


def _tool(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        [get_tool_path(args[0]), *args[1:]],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _ftdna_bam(path: Path, fmt: str = "-b", ref: Path | None = None) -> str:
    """A BAM (or CRAM) whose read names have spaces, as FTDNA Big Y BAMs do."""
    sam = (
        "@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chrY\tLN:300\n"
        "@RG\tID:rg1\tSM:kit123\n"
        f"FTDNA read 7\t99\tchrY\t101\t60\t20M\t=\t131\t50\t{_SEQ}\t{_QUAL}\tRG:Z:rg1\n"
        f"FTDNA read 9\t0\tchrY\t111\t60\t20M\t*\t0\t0\t{_GENOME[110:130]}\t{_QUAL}"
        "\tRG:Z:rg1\tNM:i:0\n"
        f"FTDNA read 7\t147\tchrY\t131\t60\t20M\t=\t101\t-50\t{_GENOME[130:150]}"
        f"\t{_QUAL}\tRG:Z:rg1\n"
    )
    ref_opt = ["-T", str(ref)] if ref else []
    _tool("samtools", "view", fmt, *ref_opt, "-o", str(path), "-", stdin=sam)
    return str(path)


def _records(path: str, *opts: str) -> list[list[str]]:
    return [
        line.split("\t")
        for line in _tool("samtools", "view", *opts, path).split("\n")
        if line
    ]


def test_repair_bam_replaces_spaces_in_read_names_only(tmp_path):
    bam = _ftdna_bam(tmp_path / "bigy.bam")
    run_wgsextract(
        "repair", "ftdna-bam", "--input", bam, "--outdir", str(tmp_path / "o")
    )
    out = str(tmp_path / "o" / "bigy_repaired.bam")
    assert [r[0] for r in _records(out)] == [
        "FTDNA:read:7",
        "FTDNA:read:9",
        "FTDNA:read:7",
    ]
    assert [r[1:] for r in _records(out)] == [r[1:] for r in _records(bam)]
    header = _tool("samtools", "view", "-H", out)
    assert "@SQ\tSN:chrY\tLN:300" in header and "@RG\tID:rg1\tSM:kit123" in header
    # Mates still pair up under the repaired name.
    assert _tool("samtools", "view", "-c", "-f", "1", out).strip() == "2"


def test_repair_bam_reads_cram_with_reference(tmp_path):
    ref = write_fasta(tmp_path / "ref.fa", {"chrY": _GENOME})
    cram = _ftdna_bam(tmp_path / "bigy.cram", "-C", ref)
    out = tmp_path / "fixed.bam"
    run_wgsextract(
        "repair", "ftdna-bam", "--input", cram, "--ref", str(ref), "--output", str(out)
    )
    assert [r[9] for r in _records(str(out))] == [
        r[9] for r in _records(cram, "-T", str(ref))
    ]
    assert _records(str(out))[0][0] == "FTDNA:read:7"


def test_repair_bam_cram_with_missing_reference_fails(tmp_path):
    cram = tmp_path / "bigy.cram"
    cram.write_bytes(b"")
    with pytest.raises(WGSExtractError, match="No reference FASTA"):
        run_wgsextract(
            "repair", "ftdna-bam", "--input", str(cram), "--ref",
            str(tmp_path / "nope.fa"),
        )  # fmt: skip


def test_repair_bam_unreadable_input_fails_without_output(tmp_path):
    bad = tmp_path / "bad.bam"
    bad.write_text("not a bam\n")
    with pytest.raises(WGSExtractError, match="FTDNA BAM repair failed"):
        run_wgsextract("repair", "ftdna-bam", "--input", str(bad))
    assert not (tmp_path / "bad_repaired.bam").exists()


def test_repair_bam_unreadable_input_names_the_reader(tmp_path):
    bad = tmp_path / "bad.bam"
    bad.write_text("not a bam\n")
    with pytest.raises(WGSExtractError, match=r"samtools view -h \(exit"):
        run_wgsextract("repair", "ftdna-bam", "--input", str(bad))


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="POSIX non-root"
)
def test_repair_bam_writer_failure_is_reported(tmp_path):
    bam = _ftdna_bam(tmp_path / "ok.bam")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(WGSExtractError, match=r"samtools view -b \(exit"):
            run_wgsextract(
                "repair", "ftdna-bam", "--input", bam, "--output", str(locked / "y.bam")
            )
    finally:
        locked.chmod(0o700)
    assert list(locked.iterdir()) == []


def test_repair_bam_requires_existing_input(tmp_path):
    with pytest.raises(WGSExtractError, match="does not exist"):
        run_wgsextract("repair", "ftdna-bam", "--input", str(tmp_path / "x.bam"))


def test_repair_streams_stdin_to_stdout_without_input(monkeypatch):
    sam = "@SQ\tSN:chrY\tLN:300\nA B C\t4\t*\t0\t0\t*\t*\t0\t0\tAC\tII\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(sam))
    monkeypatch.setattr(sys, "stdout", out := io.StringIO())
    run_wgsextract("repair", "ftdna-bam")
    assert out.getvalue() == sam.replace("A B C", "A:B:C")

    vcf = "##x=a b\n#CHROM\tPOS\nchr1\t5\t.\tA\tG\t9\tLow Qual\t.\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(vcf))
    monkeypatch.setattr(sys, "stdout", out := io.StringIO())
    run_wgsextract("repair", "ftdna-vcf")
    assert out.getvalue() == vcf.replace("Low Qual", "LowQual")


_VCF = (
    "##fileformat=VCFv4.2\n##contig=<ID=chrY,length=300>\n"
    '##FILTER=<ID=PASS,Description="All filters passed">\n'
    '##FILTER=<ID=LowQual,Description="low">\n'
    '##FILTER=<ID=q10,Description="q">\n'
    '##INFO=<ID=DP,Number=1,Type=Integer,Description="d">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tkit123\n"
    "chrY\t101\trs1\tA\tG\t50\tPASS\tDP=7\tGT\t1\n"
    "chrY\t105\t.\tT\tC\t3\tLow Qual;q<10\tDP=2\tGT\t1\n"
)


def _filters(vcf: Path) -> list[str]:
    return _tool(
        "bcftools", "query", "-f", "%POS %FILTER %INFO/DP [%GT]\n", str(vcf)
    ).splitlines()


@pytest.mark.parametrize("suffix", [".vcf", ".vcf.gz"])
def test_repair_vcf_cleans_filter_column_so_bcftools_reads_it(tmp_path, suffix):
    src = tmp_path / "kit.vcf"
    src.write_text(_VCF)
    if suffix == ".vcf.gz":
        src = Path(bgzip_index(src))
    run_wgsextract("repair", "ftdna-vcf", "--input", str(src))
    out = tmp_path / "kit_repaired.vcf"
    assert _filters(out) == ["101 PASS 7 1", "105 LowQual;q10 2 1"]
    assert out.read_text().splitlines()[:7] == _VCF.splitlines()[:7]


def test_repair_vcf_converts_bcf_input(tmp_path):
    plain = tmp_path / "kit.vcf"
    plain.write_text(_VCF.replace("Low Qual;q<10", "LowQual"))
    bcf = tmp_path / "kit.bcf"
    _tool("bcftools", "view", "-Ob", "-o", str(bcf), str(plain))
    out = tmp_path / "fixed" / "kit.vcf"
    run_wgsextract("repair", "ftdna-vcf", "--input", str(bcf), "--output", str(out))
    assert _filters(out) == ["101 PASS 7 1", "105 LowQual 2 1"]


def test_repair_vcf_leaves_short_lines_and_headers_alone(tmp_path):
    stream = io.StringIO()
    text = "##meta=a b<c>\nchrY\t1\t.\tA\tG\n" + "chrY\t2\t.\tA\tG\t1\tq 1,x\t.\n"
    repair.repair_vcf_stream(io.StringIO(text), stream)
    assert stream.getvalue() == text.replace("q 1,x", "q1x")


def test_repair_vcf_requires_existing_input(tmp_path):
    with pytest.raises(WGSExtractError, match="does not exist"):
        run_wgsextract("repair", "ftdna-vcf", "--input", str(tmp_path / "x.vcf"))


def test_repair_vcf_in_place_keeps_the_records(tmp_path):
    vcf = tmp_path / "kit.vcf"
    vcf.write_text(_VCF)
    try:
        run_wgsextract("repair", "ftdna-vcf", "--input", str(vcf), "--output", str(vcf))
    except WGSExtractError:
        pass  # refusing is fine; destroying the input is not
    assert vcf.read_text().count("chrY\t") == 2


def test_repair_cram_finds_the_reference_beside_it(tmp_path, monkeypatch):
    elsewhere = tmp_path / "made_on_another_machine"
    elsewhere.mkdir()
    ref = write_fasta(elsewhere / "ref.fa", {"chrY": _GENOME})
    kit = tmp_path / "kit"
    kit.mkdir()
    cram = _ftdna_bam(kit / "bigy.cram", "-C", ref)
    shutil.copy(ref, kit / "ref.fa")
    shutil.copy(f"{ref}.fai", kit / "ref.fa.fai")
    shutil.rmtree(elsewhere)
    # Keep htslib off the network: only an (empty) local reference cache.
    monkeypatch.setenv("REF_PATH", str(tmp_path / "no_cache" / "%s"))
    # A configured (here empty) reference library, as every install has.
    (tmp_path / "library").mkdir()
    monkeypatch.setitem(settings, "reference_library", str(tmp_path / "library"))
    run_wgsextract("repair", "ftdna-bam", "--input", cram)
    assert _records(str(kit / "bigy_repaired.bam"))[0][0] == "FTDNA:read:7"
