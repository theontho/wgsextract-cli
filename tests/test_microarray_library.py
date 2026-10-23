"""microarray on a library set up the normal way (`ref bootstrap` + a genome).

The test genome ships its own target file, so the rest of the suite never ran
microarray against a library: there it once resolved the bootstrap's chrY-only
SNP set as its target and wrote kits of nothing but no-calls. This builds a small
library with the bootstrap's layout (chip templates, no target file) and runs the
command end to end.
"""

import random
import subprocess
import sys
from pathlib import Path

import pytest

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import run_command

pytestmark = pytest.mark.skipif(
    not all(get_tool_path(t) for t in ("bcftools", "bgzip", "tabix", "samtools")),
    reason="bcftools/htslib/samtools missing",
)

# rsID, GRCh37 position on chr1, and the sample's genotype there.
MARKERS = [("rs101", 1000, "AC"), ("rs102", 2000, "CC"), ("rs103", 3000, "AA")]


def _library(root: Path) -> tuple[Path, str]:
    """A reference library: genomes/hg19.fa (a 5 kb chr1, each marker an A) and
    the 23andMe v3 chip template, as `ref bootstrap` lays them out."""
    rng = random.Random(7)
    seq = [rng.choice("ACGT") for _ in range(5000)]
    for _, pos, _ in MARKERS:
        seq[pos - 1] = "A"
    genomes = root / "genomes"
    genomes.mkdir(parents=True)
    fasta = genomes / "hg19.fa"
    fasta.write_text(">chr1\n" + "".join(seq) + "\n")
    run_command([get_tool_path("samtools"), "faidx", str(fasta)])
    templates = root / "microarray" / "raw_file_templates"
    (templates / "head").mkdir(parents=True)
    (templates / "body").mkdir()
    (templates / "head" / "23andMe_V3.txt").write_text(
        "# rsid\tchromosome\tposition\tgenotype\n"
    )
    (templates / "body" / "23andMe_V3.txt").write_text(
        "".join(f"{rsid}\t1\t{pos}\t\n" for rsid, pos, _ in MARKERS)
    )
    # The bootstrap's Y-SNP set: once resolved as the genome-wide target.
    y_snps = root / "snps_hg19.vcf"
    y_snps.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chrY,length=59373566>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chrY\t2655180\tM42\tG\tA\t.\tPASS\t.\n"
    )
    run_command([get_tool_path("bgzip"), "-f", str(y_snps)])
    run_command([get_tool_path("tabix"), "-p", "vcf", str(y_snps) + ".gz"])
    return fasta, "".join(seq)


def _sample_bam(root: Path, seq: str) -> str:
    """40 reads over each marker carrying the MARKERS genotypes."""
    lines = ["@SQ\tSN:chr1\tLN:5000", "@RG\tID:s\tSM:sample"]
    for rsid, pos, genotype in MARKERS:
        for i in range(40):
            start = pos - 60 + i
            read = list(seq[start - 1 : start - 1 + 100])
            read[pos - start] = genotype[i % 2]
            lines.append(
                f"{rsid}_{i}\t0\tchr1\t{start}\t60\t100M\t*\t0\t0\t{''.join(read)}\t"
                f"{'I' * 100}\tRG:Z:s"
            )
    sam = root / "sample.sam"
    sam.write_text("\n".join(lines) + "\n")
    bam = root / "sample.bam"
    samtools = get_tool_path("samtools")
    run_command([samtools, "sort", "-o", str(bam), str(sam)])
    run_command([samtools, "index", str(bam)])
    return str(bam)


def test_microarray_builds_its_target_and_calls_the_chip_markers(
    tmp_path: Path,
) -> None:
    library = tmp_path / "reference"
    fasta, seq = _library(library)
    bam = _sample_bam(tmp_path, seq)
    config = tmp_path / "config.toml"
    config.write_text(f'reference_library = "{library.as_posix()}"\n')
    outdir = tmp_path / "out"

    subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", "--config", str(config)]
        + ["microarray", "--formats", "23andme_v3", "--input", bam]
        + ["--ref", str(fasta), "--outdir", str(outdir)],
        check=True,
    )

    # The genome-wide target was built from the templates and kept in the library.
    assert (library / "microarray" / "All_SNPs_hg19_ref.tab.gz.tbi").is_file()
    kit = {
        line.split("\t")[0]: line.split("\t")[3].strip()
        for line in (outdir / "sample_23andMe_V3.txt").read_text().splitlines()
        if not line.startswith("#")
    }
    assert kit == {rsid: genotype for rsid, _, genotype in MARKERS}
