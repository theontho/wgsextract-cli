import random
import shutil
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


needs_pet_tools = needs_tools("bwa", "samtools", "bcftools", "bgzip")


def _dog(tmp_path: Path, fasta: str = "dog.fa") -> tuple[Path, Path]:
    """A 5 kb reference and 200 single-end reads from it, rex_R1.fq."""
    rng = random.Random(5)
    genome = "".join(rng.choice("ACGT") for _ in range(5_000))
    ref = write_fasta(tmp_path / fasta, {"chr1": genome})
    reads = tmp_path / "rex_R1.fq"
    write_read_pairs(genome, reads, tmp_path / "rex_R2.fq", range(0, 4_600, 23))
    return ref, reads


def _programs(bam: Path) -> list[str]:
    header = run_command(["samtools", "view", "-H", str(bam)], capture_output=True)
    return [line for line in header.stdout.splitlines() if line.startswith("@PG")]


@needs_pet_tools
def test_species_reference_comes_from_the_library(tmp_path):
    library = tmp_path / "lib"
    _, reads = _dog(tmp_path)
    with pytest.raises(WGSExtractError, match="cat not found.*ref library --install"):
        pet.run(_args(reads, library, species="cat"))

    plain, _ = _dog(
        library / "genomes", "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna"
    )
    run_command(["bgzip", str(plain)])
    fasta = f"{plain}.gz"
    pet.run(_args(reads, library, species="cat"))

    bam = tmp_path / "rex_R1_cat.bam"
    rg = "@RG\\tID:rex_R1\\tSM:rex\\tLB:rex\\tPL:ILLUMINA"
    # contract: bwa's -K batch size keeps alignments the same whatever -t
    bwa = f"CL:bwa mem -K 100000000 -t 2 -R {rg} {fasta} {reads}"
    assert any(bwa in line for line in _programs(bam))
    assert Path(f"{fasta}.bwt").is_file()  # indexed in the library
    assert (tmp_path / "rex_R1_cat.vcf.gz.csi").is_file()


@needs_pet_tools
@pytest.mark.parametrize(
    ("memory", "sort_memory"), [("512M", "256M"), ("128M", "128M")]
)
def test_bwa_mem2_aligns_when_the_reference_has_its_index(
    tmp_path, memory, sort_memory
):
    ref, reads = _dog(tmp_path)
    if not shutil.which("bwa-mem2"):
        pytest.skip("bwa-mem2 missing")
    run_command(["bwa-mem2", "index", str(ref)])

    pet.run(_args(reads, ref, memory=memory))

    programs = _programs(tmp_path / "rex_R1_dog.bam")
    assert any("CL:bwa-mem2 mem -K 100000000 " in line for line in programs)
    # bwa-mem2 holds more memory, so its sort gets less, down to a floor.
    sort = next(line for line in programs if "CL:samtools sort" in line)
    assert f" -m {sort_memory} " in sort
    assert not Path(f"{ref}.bwt").exists()  # no bwa index built


@needs_pet_tools
def test_missing_reads_or_reference_are_reported(tmp_path):
    _, reads = _dog(tmp_path)
    before = sorted(tmp_path.iterdir())
    with pytest.raises(WGSExtractError, match="--r1 is required"):
        pet.run(_args(None, tmp_path))
    with pytest.raises(WGSExtractError, match="--r2"):
        pet.run(_args(reads, tmp_path, r2=str(tmp_path / "gone.fq")))
    with pytest.raises(WGSExtractError, match="--ref"):
        pet.run(_args(reads, None))
    with pytest.raises(WGSExtractError, match="not found at .*gone\\.fa\\.$"):
        pet.run(_args(reads, tmp_path / "gone.fa"))
    assert sorted(tmp_path.iterdir()) == before


@needs_pet_tools
def test_alignment_failure_is_reported(tmp_path):
    ref, _ = _dog(tmp_path)
    reads = tmp_path / "rex_R1.fq"
    reads.chmod(0)  # bwa can't read it
    with pytest.raises(WGSExtractError, match="Alignment failed in bwa mem"):
        pet.run(_args(reads, ref, outdir=str(tmp_path / "out")))
    assert list((tmp_path / "out").iterdir()) == []


@needs_pet_tools
def test_failed_variant_calling_leaves_no_partial_vcf(tmp_path):
    ref, reads = _dog(tmp_path)
    run_command(["samtools", "faidx", str(ref)])
    Path(f"{ref}.fai").write_text("chr1\tnot a length\n")
    out = tmp_path / "out"
    with pytest.raises(
        WGSExtractError, match="Variant calling failed in bcftools mpileup"
    ):
        pet.run(_args(reads, ref, outdir=str(out)))
    assert sorted(p.name for p in out.iterdir()) == [
        "rex_R1_dog.bam",
        "rex_R1_dog.bam.bai",
    ]
