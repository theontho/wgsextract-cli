"""Native, not rebased, CHM13 WGS against independently measured chip truth."""

import hashlib
import sys
from pathlib import Path
from urllib.request import urlopen

import pytest

from tests.htslib_helpers import needs_tools
from tests.test_remote_benchmark import OMNI, REGION_HG19, _run, _score_all_exports
from wgsextract_cli.commands.benchmark.chip import CONSUMER_FORMATS
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.microarray_chm13 import load_chm13_catalogue
from wgsextract_cli.core.microarray_site_map import SiteMap
from wgsextract_cli.core.microarray_targets import ncbi36_chain

pytestmark = [
    pytest.mark.remote,
    needs_tools("bcftools", "samtools", "tabix"),
]

CHM13_FASTA = (
    "https://s3-us-west-2.amazonaws.com/human-pangenomics/T2T/CHM13/"
    "assemblies/analysis_set/chm13v2.0_maskedY.fa.gz"
)
CHM13_FASTA_SHA256 = "36b83c984efe3db0ad05065e875e72fdcc56ade32b5c61cbffc14698453125a3"
HG00096_CHM13_CRAM = (
    "https://anvilproject.s3.amazonaws.com/file/71d42a36eb86dbf3b0bcca7fe02eb864.md5"
)
HG00096_CHM13_CRAI = (
    "https://anvilproject.s3.amazonaws.com/file/62e0f69e223ab4abf17f92fe5b326826.md5"
)
CHM13_CRAI_SHA256 = "b876a441a4eee5bee147653343af03bac9f4426513002af5d447a1b63c26a96a"
REGION_CHM13 = "chr20:10000000-12000000"


def _in_native_called_region(chrom: str, pos: str, mapping: SiteMap) -> bool:
    site = mapping.source_lookup(f"chr{chrom}", int(pos))
    return (
        site is not None
        and site.status == "mapped"
        and site.target_chrom == "chr20"
        and site.target_pos is not None
        and 10_000_000 <= site.target_pos <= 12_000_000
    )


def _download_checked(url: str, destination: Path, sha256: str) -> None:
    digest = hashlib.sha256()
    with urlopen(url, timeout=120) as response, destination.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
            digest.update(chunk)
    assert digest.hexdigest() == sha256


def test_native_chm13_kit_matches_its_omni_chip(tmp_path: Path) -> None:
    samtools, bcftools = get_tool_path("samtools"), get_tool_path("bcftools")
    cli = [sys.executable, "-m", "wgsextract_cli.main"]
    library = tmp_path / "reference"
    _run([*cli, "ref", "bootstrap", "--ref", str(library)], tmp_path)
    ncbi36_chain(str(library))
    fasta = library / "genomes" / "chm13v2.0_maskedY.fa.gz"
    fasta.parent.mkdir(exist_ok=True)
    _download_checked(CHM13_FASTA, fasta, CHM13_FASTA_SHA256)
    _run([samtools, "faidx", str(fasta)], tmp_path)
    index = tmp_path / "HG00096.cram.crai"
    _download_checked(HG00096_CHM13_CRAI, index, CHM13_CRAI_SHA256)
    # Keep the full SQ header: the runtime verifies every primary length/M5,
    # including the masked Y and original MT, even for this chr20-only slice.
    bam = tmp_path / "HG00096.bam"
    _run(
        [samtools, "view", "-b", "-T", str(fasta), "-o", str(bam)]
        + [f"{HG00096_CHM13_CRAM}##idx##{index}", REGION_CHM13],
        tmp_path,
    )
    _run([samtools, "index", str(bam)], tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(f'reference_library = "{library.as_posix()}"\n', newline="\n")
    outdir = tmp_path / "kit"
    _run(
        [*cli, "--config", str(config), "microarray", "--formats"]
        + [
            ",".join(
                name.lower().replace("23andme_snps_api", "23andme_api")
                for name in CONSUMER_FORMATS
            )
        ]
        + ["--region", REGION_CHM13, "--input", str(bam), "--ref", str(fasta)]
        + ["--outdir", str(outdir), "--threads", "2"],
        tmp_path,
    )
    chip_rows = _run(
        [bcftools, "query", "-s", "HG00096,HG00097", "-r", REGION_HG19]
        + ["-f", "%ID\t%CHROM\t%POS[\t%TGT]\n", OMNI],
        tmp_path,
    )
    mapping = load_chm13_catalogue().mapping
    truth_rows: list[str] = []
    control_rows: list[str] = []
    for row in chip_rows.splitlines():
        rsid, chrom, pos, genotype, control = row.split("\t")
        # Coordinate-only eligibility, before inspecting any kit genotype.
        if rsid.startswith("rs") and _in_native_called_region(chrom, pos, mapping):
            truth_rows.append(f"{rsid}\t{chrom}\t{pos}\t{genotype}\n")
            control_rows.append(f"{rsid}\t{chrom}\t{pos}\t{control}\n")
    chip_file, control_file = tmp_path / "chip.tsv", tmp_path / "control.tsv"
    chip_file.write_text("".join(truth_rows), newline="\n")
    control_file.write_text("".join(control_rows), newline="\n")
    _score_all_exports(
        outdir, "HG00096", chip_file, control_file, tmp_path / "chip-concordance.json"
    )
