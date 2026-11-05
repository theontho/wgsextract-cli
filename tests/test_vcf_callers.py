"""`vcf freebayes` and `vcf gatk` (CPU HaplotypeCaller) on a tiny genome."""

import json
import os
import platform
import random
import shutil
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

from tests.fake_tools import FakeGatk
from tests.htslib_helpers import (
    needs_tools,
    require_runnable,
    run_wgsextract,
    sam_to_bam,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli.commands.vcf import callers
from wgsextract_cli.core import paths
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.main import main

SNP = 1000  # 1-based position of the sample's homozygous SNP


@pytest.fixture(scope="module")
def genome(tmp_path_factory) -> Namespace:
    """A 3 kb chr1, and a sample homozygous for one SNP at SNP, as a BAM and a
    CRAM with 20x single-end coverage over 500-2500."""
    work = tmp_path_factory.mktemp("callers")
    rng = random.Random(7)
    ref = "".join(rng.choice("ACGT") for _ in range(3000))
    alt = {"A": "C", "C": "G", "G": "T", "T": "A"}[ref[SNP - 1]]
    sample = ref[: SNP - 1] + alt + ref[SNP:]
    fasta = write_fasta(work / "ref.fa", {"chr1": ref})
    lines = ["@HD\tVN:1.6\tSO:unsorted", "@SQ\tSN:chr1\tLN:3000", "@RG\tID:rg\tSM:S"]
    for i, start in enumerate(range(500, 2400, 5)):
        lines.append(
            f"r{i}\t{16 if i % 2 else 0}\tchr1\t{start}\t60\t100M\t*\t0\t0\t"
            f"{sample[start - 1 : start + 99]}\t{'I' * 100}\tRG:Z:rg"
        )
    bam = sam_to_bam(lines, work / "s.bam")
    cram = str(work / "s.cram")
    run_command(["samtools", "view", "-C", "-T", str(fasta), "-o", cram, bam])
    run_command(["samtools", "index", cram])
    return Namespace(
        ref=str(fasta),
        bam=bam,
        cram=cram,
        call=("chr1", str(SNP), ref[SNP - 1], alt, "1/1"),
    )


def _call(caller: str, genome: Namespace, input_path: str, outdir: Path, *extra: str):
    require_runnable(caller, "samtools", "tabix")
    run_wgsextract("vcf", caller, "--input", input_path, "--ref", genome.ref,
                   "--outdir", str(outdir), *extra)  # fmt: skip
    vcf = outdir / f"{caller}.vcf.gz"
    assert Path(f"{vcf}.tbi").is_file()
    return [call[:5] for call in vcf_calls(vcf)]


# --- freebayes -------------------------------------------------------------------

needs_freebayes = needs_tools("freebayes", "samtools", "bcftools", "tabix")


@needs_freebayes
def test_freebayes_calls_the_snp_from_a_bam(genome, tmp_path):
    assert _call("freebayes", genome, genome.bam, tmp_path) == [genome.call]


@needs_freebayes
def test_freebayes_streams_a_cram_through_samtools(genome, tmp_path):
    assert _call("freebayes", genome, genome.cram, tmp_path) == [genome.call]


@needs_freebayes
def test_freebayes_decompresses_a_gzipped_reference_and_removes_it(genome, tmp_path):
    require_runnable("freebayes", "samtools", "tabix")
    gz = tmp_path / "ref" / "ref.fa.gz"
    gz.parent.mkdir()
    with open(gz, "wb") as f:
        run_command(["bgzip", "-c", genome.ref], stdout=f)
    run_command(["samtools", "faidx", str(gz)])
    outdir = tmp_path / "out"
    run_wgsextract("vcf", "freebayes", "--input", genome.bam, "--ref", str(gz),
                   "--outdir", str(outdir))  # fmt: skip
    assert vcf_calls(outdir / "freebayes.vcf.gz") == [genome.call]
    assert not list(outdir.glob(".wgse_ref_*"))
    assert not (gz.parent / "ref.fa").exists()


@needs_freebayes
def test_freebayes_with_nothing_in_the_region_warns(genome, tmp_path, caplog):
    assert (
        _call("freebayes", genome, genome.bam, tmp_path, "-r", "chr1:2000-2100") == []
    )
    assert "No variants were called in chr1:2000-2100" in caplog.text


@needs_freebayes
def test_freebayes_bam_region_includes_its_first_base(genome, tmp_path):
    region = f"chr1:{SNP}-{SNP + 50}"
    assert _call("freebayes", genome, genome.bam, tmp_path, "-r", region) == [
        genome.call
    ]


@needs_freebayes
def test_freebayes_cram_region_excludes_calls_outside_it(genome, tmp_path):
    region = f"chr1:{SNP + 10}-{SNP + 50}"
    assert _call("freebayes", genome, genome.cram, tmp_path, "-r", region) == []


# --- GATK HaplotypeCaller --------------------------------------------------------


@needs_tools("gatk", "samtools", "tabix")
def test_gatk_calls_the_snp_from_a_regional_cram(genome, tmp_path):
    """One real GATK run (slow to start): the CRAM is cut to a temporary BAM,
    the missing .dict is made beside the reference, and nothing else is left."""
    require_runnable("gatk", "samtools", "tabix")
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    ref = ref_dir / "ref.fa"
    ref.write_text(Path(genome.ref).read_text())
    run_command(["samtools", "faidx", str(ref)])
    outdir = tmp_path / "out"
    run_wgsextract(
        "vcf", "gatk", "--input", Path(genome.cram).as_posix(),
        "--ref", ref.as_posix(), "--outdir", outdir.as_posix(),
        "-r", "chr1:900-1100",
    )  # fmt: skip

    assert [c[:5] for c in vcf_calls(outdir / "gatk.vcf.gz")] == [genome.call]
    assert (ref_dir / "ref.dict").read_text().startswith("@HD")
    assert sorted(p.name for p in outdir.iterdir()) == [
        "gatk.vcf.gz",
        "gatk.vcf.gz.tbi",
    ]


# --- GATK at its process boundary ----------------------------------------------
# GATK is slow to start and not in the default environment: FakeGatk calls with
# bcftools instead and checks what GATK itself would refuse.


@pytest.fixture
def gatk(tmp_path, monkeypatch) -> FakeGatk:
    require_runnable("samtools", "bcftools", "tabix")
    bcftools = shutil.which("bcftools") or pytest.skip("bcftools is not on PATH")
    bindir = tmp_path / "bin"
    fake = FakeGatk(bindir, bcftools)
    # The OS temp folder, where GATK's temp dir goes without a --tmp-dir.
    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systemp"))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    # The fake runs anywhere; real GATK is refused on Linux ARM64 (#236).
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    clear_tool_caches()
    return fake


def _reference_copy(genome: Namespace, folder: Path, *, gzip: bool = False) -> str:
    folder.mkdir(parents=True)
    ref = folder / "ref.fa"
    shutil.copy(genome.ref, ref)
    if gzip:
        with open(f"{ref}.gz", "wb") as f:
            run_command(["bgzip", "-c", str(ref)], stdout=f)
        ref.unlink()
        ref = folder / "ref.fa.gz"
    run_command(["samtools", "faidx", str(ref)])
    return str(ref)


def _gatk(capsys, genome: Namespace, input_path: str, ref: str, outdir: Path,
          scratch: Path | None, *extra: str) -> tuple[dict, str]:  # fmt: skip
    """`vcf gatk` through the CLI: its --json result manifest and its log.
    Without a scratch folder the temporary BAM goes beside the output."""
    if scratch:
        scratch.mkdir(exist_ok=True)
        extra = ("--tmp-dir", str(scratch), *extra)
    try:
        main(["vcf", "gatk", "--input", input_path, "--ref", ref,
              "--outdir", str(outdir), *extra, "--json"])  # fmt: skip
    except SystemExit:
        pass
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def test_gatk_cuts_a_regional_cram_to_a_bam_it_can_read(genome, gatk, tmp_path, capsys):
    """A GATK that can't read CRAM 3.1 still calls a CRAM region: the CRAM is cut
    to a temporary BAM of every requested interval (given out of order), and
    the missing .dict is made beside the reference. Nothing else is left beside
    the output or in the OS temp folder."""
    gatk.set(reads_cram=False)
    ref = _reference_copy(genome, tmp_path / "ref")
    outdir = tmp_path / "out"
    result, log = _gatk(capsys, genome, genome.cram, ref, outdir, None,
                   "-r", f"chr1:1500-1600, chr1:{SNP - 100}-{SNP + 100}")  # fmt: skip

    assert result["status"] == "ok", result["error"]
    assert vcf_calls(outdir / "gatk.vcf.gz") == [genome.call]
    assert (tmp_path / "ref" / "ref.dict").read_text().startswith("@HD")
    assert sorted(p.name for p in outdir.iterdir()) == [
        "gatk.vcf.gz",
        "gatk.vcf.gz.tbi",
        "gatk.vcf.manifest.json",
    ]
    assert list((tmp_path / "systemp").iterdir()) == []


@pytest.mark.parametrize("stage", ["gatk", "conversion"])
def test_gatk_failure_on_a_cram_hints_at_cram_versions_and_cleans_up(
    genome, gatk, tmp_path, capsys, stage
):
    """Whether GATK or the CRAM-to-BAM cut fails (a reference that doesn't match
    the CRAM fails its MD5 check part-way), the partial or temporary BAM, its
    index, the intervals and GATK's temp dir are removed."""
    gatk.set(fail="unexpected CRAM container")
    ref = _reference_copy(genome, tmp_path / "ref")
    if stage == "conversion":
        seq = "".join(Path(ref).read_text().splitlines()[1:])
        write_fasta(Path(ref), {"chr1": seq[::-1]})
        run_command(["samtools", "faidx", ref])
    outdir = tmp_path / "out"
    result, log = _gatk(capsys, genome, genome.cram, ref, outdir, None,
                   "-r", f"chr1:{SNP - 100}-{SNP + 100}")  # fmt: skip

    assert result["status"] == "failed"
    assert "GATK failed" in result["error"]["message"]
    assert "CRAM version 3.1" in log
    assert (gatk.runs() == []) == (stage == "conversion")
    assert list(outdir.iterdir()) == []
    assert list((tmp_path / "systemp").iterdir()) == []


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a folder the user can't write to",
)
def test_gatk_reports_a_dict_it_cannot_write(genome, gatk, tmp_path, capsys):
    ref = _reference_copy(genome, tmp_path / "ref")
    (tmp_path / "ref").chmod(0o555)
    try:
        result, log = _gatk(capsys, genome, genome.bam, ref, tmp_path / "out",
                       tmp_path / "scratch")  # fmt: skip
    finally:
        (tmp_path / "ref").chmod(0o755)
    assert result["status"] == "failed"
    assert "Failed to generate GATK dict" in result["error"]["message"]
    assert gatk.runs() == []


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_gatk_on_windows_gets_native_paths_and_a_numeric_warning(
    genome, gatk, tmp_path, monkeypatch, capsys, platform
):
    """Windows GATK reads only native paths (a bare R:/x is a URI to Java), here
    with spaces and a bgzipped reference; its native kernels can change numbers."""
    monkeypatch.setattr(paths, "sys", Namespace(platform=platform))
    monkeypatch.setattr(callers, "sys", Namespace(platform=platform))
    gatk.set(windows=platform == "win32")
    ref = _reference_copy(genome, tmp_path / "Reference genome", gzip=True)
    # A missing .dict would be written by the host's samtools at a native path.
    run_command(["samtools", "dict", "-o", str(tmp_path / "Reference genome" / "ref.dict"), ref])  # fmt: skip
    outdir = tmp_path / "Output data"
    result, log = _gatk(capsys, genome, genome.bam, ref, outdir, tmp_path / "scratch",
                   "-r", f"chr1:{SNP - 100}-{SNP + 100}")  # fmt: skip

    assert result["status"] == "ok", result["error"]
    assert vcf_calls(outdir / "gatk.vcf.gz") == [genome.call]
    assert ("compression remains Java-based" in log) == (platform == "win32")
