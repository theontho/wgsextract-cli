import random
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, write_fasta, write_read_pairs
from wgsextract_cli.commands import pet
from wgsextract_cli.core.utils import WGSExtractError, run_command

SNP = 2_500  # 0-based position of the planted SNP


def _args(r1, ref, **overrides):
    values = {
        "r1": str(r1) if r1 else None,
        "r2": None,
        "ref": str(ref) if ref else None,
        "species": "dog",
        "format": "BAM",
        "outdir": None,
        "threads": "2",
        "memory": "100M",
    }
    values.update(overrides)
    return Namespace(**values)


@needs_tools("bwa", "samtools", "bcftools")
@pytest.mark.parametrize(("paired", "fmt"), [(True, "BAM"), (False, "CRAM")])
def test_pet_align_calls_a_planted_snp(tmp_path, paired, fmt):
    rng = random.Random(3)
    genome = "".join(rng.choice("ACGT") for _ in range(5_000))
    alt = "A" if genome[SNP] != "A" else "C"
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": genome})
    sample = genome[:SNP] + alt + genome[SNP + 1 :]
    starts = rng.sample(range(len(sample) - 300), 600)
    starts.append(starts[0])  # one duplicate fragment
    r1, r2 = tmp_path / "reads_R1.fq", tmp_path / "reads_R2.fq"
    write_read_pairs(sample, r1, r2, starts)
    outdir = tmp_path / "out"

    pet.run(
        _args(r1, ref, r2=str(r2) if paired else None, format=fmt, outdir=str(outdir))
    )

    # Both mates go into one output, named without the mate marker.
    stem = "reads" if paired else "reads_R1"
    out = outdir / f"{stem}_dog.{fmt.lower()}"
    assert Path(f"{out}.{'bai' if fmt == 'BAM' else 'crai'}").is_file()
    view = ["samtools", "view", "-T", str(ref)]
    header = run_command([*view, "-H", str(out)], capture_output=True).stdout
    assert "SO:coordinate" in header
    assert "\tSM:reads\t" in header  # the sample is named, not the BAM path
    reads = [
        r.split("\t")
        for r in run_command([*view, str(out)], capture_output=True).stdout.split("\n")
        if r
    ]
    assert len(reads) == len(starts) * (2 if paired else 1)
    dups = [r[0] for r in reads if int(r[1]) & 0x400]
    assert len(dups) == (2 if paired else 1)
    assert set(dups) <= {"p0", f"p{len(starts) - 1}"}

    vcf = outdir / f"{stem}_dog.vcf.gz"
    assert Path(f"{vcf}.csi").is_file()
    calls = run_command(
        ["bcftools", "query", "-f", "%CHROM\t%POS\t%REF\t%ALT\t[%GT]\n", str(vcf)],
        capture_output=True,
    ).stdout
    assert calls == f"chr1\t{SNP + 1}\t{genome[SNP]}\t{alt}\t1/1\n"


@pytest.fixture
def no_tools(monkeypatch):
    """Record the alignment and calling commands instead of running them."""
    calls = {"index": [], "align": [], "pipeline": [], "command": []}
    monkeypatch.setattr(pet, "verify_dependencies", lambda _tools: None)
    monkeypatch.setattr(pet, "log_dependency_info", lambda _tools: None)
    monkeypatch.setattr(pet, "ensure_bwa_index", calls["index"].append)
    monkeypatch.setattr(
        pet, "run_streamed_alignment", lambda cmd, out, **k: calls["align"].append(cmd)
    )
    monkeypatch.setattr(
        pet, "run_pipeline", lambda stages, **_k: calls["pipeline"].append(stages)
    )
    monkeypatch.setattr(pet, "run_command", calls["command"].append)
    return calls


@pytest.fixture
def reads(tmp_path):
    path = tmp_path / "rex_R1.fastq.gz"
    path.write_bytes(b"")
    return path


def test_species_reference_comes_from_the_library(
    tmp_path, monkeypatch, reads, no_tools
):
    monkeypatch.setattr(pet, "short_read_aligner", lambda _req, _ref: "bwa")
    library = tmp_path / "lib"
    with pytest.raises(WGSExtractError, match="cat not found.*ref library --install"):
        pet.run(_args(reads, library, species="cat"))

    fasta = library / "genomes" / "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz"
    fasta.parent.mkdir(parents=True)
    fasta.write_bytes(b"")
    pet.run(_args(reads, library, species="cat"))

    assert no_tools["index"] == [str(fasta)]
    (cmd,) = no_tools["align"]
    rg = "@RG\\tID:rex_R1\\tSM:rex\\tLB:rex\\tPL:ILLUMINA"
    assert cmd == [
        "bwa", "mem", "-K", "100000000", "-t", "2", "-R", rg, str(fasta), str(reads)
    ]  # fmt: skip
    mpileup, call = no_tools["pipeline"][0]
    assert mpileup[-3:] == ["-f", str(fasta), str(tmp_path / "rex_R1_cat.bam")]
    assert call[-1] == str(tmp_path / "rex_R1_cat.vcf.gz")


def test_bwa_mem2_aligns_when_the_reference_has_its_index(
    tmp_path, monkeypatch, reads, no_tools
):
    ref = tmp_path / "dog.fa"
    ref.write_text(">chr1\nACGT\n")
    monkeypatch.setattr(pet, "short_read_aligner", lambda req, _ref: "bwa-mem2")
    pet.run(_args(reads, ref))
    assert no_tools["index"] == []
    assert no_tools["align"][0][:2] == ["bwa-mem2", "mem"]
    command = no_tools["align"][0]
    assert command[command.index("-K") + 1] == "100000000"


def test_missing_reads_or_reference_are_reported(tmp_path, reads, no_tools):
    with pytest.raises(WGSExtractError, match="--r1 is required"):
        pet.run(_args(None, tmp_path))
    with pytest.raises(WGSExtractError, match="--r2"):
        pet.run(_args(reads, tmp_path, r2=str(tmp_path / "gone.fq")))
    with pytest.raises(WGSExtractError, match="--ref"):
        pet.run(_args(reads, None))
    assert no_tools["align"] == []


def test_mistyped_reference_fasta_is_named_in_the_error(tmp_path, reads, no_tools):
    with pytest.raises(WGSExtractError, match="not found at .*gone\\.fa\\.$"):
        pet.run(_args(reads, tmp_path / "gone.fa"))


def test_alignment_failure_is_reported(tmp_path, monkeypatch, reads, no_tools):
    ref = tmp_path / "dog.fa"
    ref.write_text(">chr1\nACGT\n")
    monkeypatch.setattr(pet, "short_read_aligner", lambda _req, _ref: "bwa")

    def fail(*_a, **_k):
        raise OSError("bwa: not found")

    monkeypatch.setattr(pet, "run_streamed_alignment", fail)
    with pytest.raises(WGSExtractError, match="Alignment error: bwa: not found"):
        pet.run(_args(reads, ref))
    assert no_tools["pipeline"] == []


def test_failed_variant_calling_leaves_no_partial_vcf(
    tmp_path, monkeypatch, reads, no_tools
):
    ref = tmp_path / "dog.fa"
    ref.write_text(">chr1\nACGT\n")
    monkeypatch.setattr(pet, "short_read_aligner", lambda _req, _ref: "bwa")
    vcf = tmp_path / "rex_R1_dog.vcf.gz"

    def fail_index(cmd):
        vcf.write_bytes(b"partial")
        Path(f"{vcf}.csi").write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(pet, "run_command", fail_index)
    with pytest.raises(WGSExtractError, match="Variant calling error"):
        pet.run(_args(reads, ref))
    assert not vcf.exists() and not Path(f"{vcf}.csi").exists()
