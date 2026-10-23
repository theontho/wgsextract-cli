"""Microarray kits against a real chip, from public data (run weekly: -m remote).

1000 Genomes sample HG00096 has a 30x whole genome (NYGC, GRCh38) and Illumina
Omni 2.5 chip genotypes. This makes kits from 2 Mb of the genome with the real
CLI and a library from `ref bootstrap`, then compares it with the chip by rsID.
Everything is read remotely by region, so the downloads stay small apart from
the bootstrap (~170 MB) and chr20 (~65 MB).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from wgsextract_cli.core.dependencies import get_tool_path

pytestmark = [
    pytest.mark.remote,
    pytest.mark.skipif(
        not all(get_tool_path(t) for t in ("bcftools", "samtools", "tabix")),
        reason="bcftools/samtools missing",
    ),
]

KG = "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp"
GRCH38 = (
    f"{KG}/technical/reference/GRCh38_reference_genome/"
    "GRCh38_full_analysis_set_plus_decoy_hla.fa"
)
HG00096_CRAM = "https://ftp.sra.ebi.ac.uk/vol1/run/ERR324/ERR3240114/HG00096.final.cram"
OMNI = (
    f"{KG}/release/20130502/supporting/hd_genotype_chip/"
    "ALL.chip.omni_broad_sanger_combined.20140818.snps.genotypes.vcf.gz"
)
REGION_HG38 = "chr20:10000000-12000000"
# The same stretch in GRCh37, with a margin; only markers the kit calls count.
REGION_HG19 = "20:9900000-12100000"
MIN_MARKERS = 200
MIN_CONCORDANCE = 0.98


def _run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _kit_genotypes(path: Path) -> dict[str, str]:
    calls = {}
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if line.startswith("#") or len(parts) < 4 or parts[3] == "--":
            continue
        calls[parts[0]] = "".join(sorted(parts[3]))
    return calls


def test_kit_from_hg00096_matches_its_omni_chip(tmp_path: Path) -> None:
    samtools, bcftools = get_tool_path("samtools"), get_tool_path("bcftools")
    cli = [sys.executable, "-m", "wgsextract_cli.main"]
    library = tmp_path / "reference"
    _run([*cli, "ref", "bootstrap", "--ref", str(library)], tmp_path)
    # chr20 of the reference NYGC aligned to, as the library's genome.
    (library / "genomes").mkdir(exist_ok=True)
    fasta = library / "genomes" / "GRCh38_chr20.fa"
    fasta.write_text(_run([samtools, "faidx", GRCH38, "chr20"], tmp_path))
    _run([samtools, "faidx", str(fasta)], tmp_path)
    # The 2 Mb slice of HG00096's 30x CRAM (htslib fetches it by region).
    bam = tmp_path / "HG00096.bam"
    _run(
        [samtools, "view", "-b", "-T", GRCH38, "-o", str(bam)]
        + [HG00096_CRAM, REGION_HG38],
        tmp_path,
    )
    _run([samtools, "index", str(bam)], tmp_path)

    config = tmp_path / "config.toml"
    config.write_text(f'reference_library = "{library.as_posix()}"\n')
    outdir = tmp_path / "kit"
    _run(
        [*cli, "--config", str(config), "microarray", "--formats", "23andme_v5"]
        + ["-r", REGION_HG38, "--input", str(bam), "--ref", str(fasta)]
        + ["--outdir", str(outdir)],
        tmp_path,
    )
    # Every chip's markers (the CombinedKit), and one vendor kit's.
    kit = _kit_genotypes(outdir / "HG00096_CombinedKit.txt")
    vendor = _kit_genotypes(outdir / "HG00096_23andMe_V5.txt")

    chip_rows = _run(
        [bcftools, "query", "-s", "HG00096", "-r", REGION_HG19]
        + ["-f", "%ID\t[%TGT]\n", OMNI],
        tmp_path,
    )
    chip = {}
    for row in chip_rows.splitlines():
        rsid, genotype = row.split("\t")
        alleles = genotype.replace("|", "/").split("/")
        if rsid.startswith("rs") and all(a in "ACGT" and a for a in alleles):
            chip[rsid] = "".join(sorted(alleles))

    for name, calls, minimum in (
        ("CombinedKit", kit, MIN_MARKERS),
        ("23andMe v5 kit", vendor, MIN_MARKERS // 2),
    ):
        shared = [rsid for rsid in calls if rsid in chip]
        agree = sum(calls[rsid] == chip[rsid] for rsid in shared)
        print(f"HG00096 {name} vs Omni chip: {agree}/{len(shared)} markers agree")
        assert len(shared) >= minimum
        assert agree / len(shared) >= MIN_CONCORDANCE
