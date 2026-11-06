import random
import shutil
from pathlib import Path

import pytest

from tests.fake_tools import only_tools, python_tool
from tests.htslib_helpers import needs_tools, write_fasta, write_read_pairs
from wgsextract_cli import api
from wgsextract_cli.core.utils import run_command

SNP = 2_500  # 0-based position of the planted SNP


def _align(r1, ref, **overrides):
    values = {"r1": r1, "ref": ref, "species": "dog", "threads": 2, "memory": "100M"}
    return api.pet.align(api.PetAlignParams(**(values | overrides)))


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

    result = _align(r1, ref, r2=r2 if paired else None, format=fmt, outdir=outdir)

    # Both mates go into one output, named without the mate marker.
    stem = "reads" if paired else "reads_R1"
    out = outdir / f"{stem}_dog.{fmt.lower()}"
    vcf = outdir / f"{stem}_dog.vcf.gz"
    assert {(Path(o.path), o.role) for o in result.outputs} >= {
        (out, "alignment"),
        (vcf, "variants"),
    }
    assert result.metrics["species"] == "dog"
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
    with pytest.raises(api.CommandFailed, match="cat not found.*ref library --install"):
        _align(reads, library, species="cat")

    plain, _ = _dog(
        library / "genomes", "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna"
    )
    run_command(["bgzip", str(plain)])
    fasta = f"{plain}.gz"
    _align(reads, library, species="cat")

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
    tmp_path, ram, memory, sort_memory
):
    ram(64 * 1024**3)  # bwa-mem2 is only chosen when its pipeline fits
    ref, reads = _dog(tmp_path)
    if not shutil.which("bwa-mem2"):
        pytest.skip("bwa-mem2 missing")
    run_command(["bwa-mem2", "index", str(ref)])

    _align(reads, ref, memory=memory)

    programs = _programs(tmp_path / "rex_R1_dog.bam")
    # The Windows launcher re-executes a CPU-specific build, which names CL.
    (aligner,) = [line for line in programs if "\tPN:bwa-mem2\t" in line]
    assert " mem -K 100000000 " in aligner
    # bwa-mem2 holds more memory, so its sort gets less, down to a floor.
    sort = next(line for line in programs if "CL:samtools sort" in line)
    assert f" -m {sort_memory} " in sort
    assert not Path(f"{ref}.bwt").exists()  # no bwa index built


@needs_pet_tools
def test_missing_reads_or_reference_are_reported(tmp_path):
    _, reads = _dog(tmp_path)
    before = sorted(tmp_path.iterdir())
    with pytest.raises(api.UsageError, match="--r1 is required"):
        api.PetAlignParams(ref=tmp_path, species="dog")
    with pytest.raises(api.CommandFailed, match="--r2"):
        _align(reads, tmp_path, r2=tmp_path / "gone.fq")
    with pytest.raises(api.CommandFailed, match="--ref"):
        _align(reads, None)
    with pytest.raises(api.CommandFailed, match="not found at .*gone\\.fa\\.$"):
        _align(reads, tmp_path / "gone.fa")
    assert sorted(tmp_path.iterdir()) == before


@needs_pet_tools
def test_alignment_failure_is_reported(tmp_path):
    ref, reads = _dog(tmp_path)
    for suffix in (".amb", ".ann", ".bwt", ".pac", ".sa"):  # a corrupt index
        Path(f"{ref}{suffix}").write_text("not an index\n")
    with pytest.raises(api.CommandFailed, match="Alignment failed in bwa mem"):
        _align(reads, ref, outdir=tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


_FAILING_INDEX = """\
import subprocess, sys
if sys.argv[1] == "index":  # the disk fills while the index is written
    open(sys.argv[-1] + ".csi", "wb").write(b"partial")
    sys.exit(1)
sys.exit(subprocess.call([{real!r}, *sys.argv[1:]]))
"""


@needs_pet_tools
def test_failed_vcf_indexing_removes_the_vcf_and_its_partial_index(tmp_path, request):
    real = shutil.which("bcftools")
    bindir = only_tools(request, "bwa", "samtools", "bgzip")
    python_tool(bindir, "bcftools", _FAILING_INDEX.format(real=real))
    ref, reads = _dog(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(api.CommandFailed, match="Variant calling error"):
        _align(reads, ref, outdir=out)
    assert sorted(p.name for p in out.iterdir()) == [
        "rex_R1_dog.bam",
        "rex_R1_dog.bam.bai",
        "rex_R1_dog.bam.manifest.json",
    ]


@needs_pet_tools
def test_failed_variant_calling_leaves_no_partial_vcf(tmp_path):
    ref, reads = _dog(tmp_path)
    run_command(["samtools", "faidx", str(ref)])
    Path(f"{ref}.fai").write_text("chr1\tnot a length\n")
    out = tmp_path / "out"
    with pytest.raises(
        api.CommandFailed, match="Variant calling failed in bcftools mpileup"
    ):
        _align(reads, ref, outdir=out)
    assert sorted(p.name for p in out.iterdir()) == [
        "rex_R1_dog.bam",
        "rex_R1_dog.bam.bai",
        "rex_R1_dog.bam.manifest.json",
    ]
