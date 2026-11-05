"""Regressions found in code review: annotation resources and build names."""

import os
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.commands.vcf.annotation_helpers import prepare_tabix_annotation
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import run_command


def _bgzip_tsv(path: Path, rows: list[str]) -> str:
    plain = path.with_suffix("")
    plain.write_text("".join(f"{row}\n" for row in rows))
    with open(path, "wb") as out:
        run_command(["bgzip", "-c", str(plain)], stdout=out)
    plain.unlink()
    return str(path)


def _rows_at(resource: str, region: str) -> list[str]:
    return run_command(
        ["tabix", resource, region], capture_output=True
    ).stdout.splitlines()


@needs_tools("bgzip", "tabix")
def test_tabix_annotation_is_indexed_by_its_coordinate_columns(tmp_path):
    """A per-position TSV (chrom, pos, ...) answers region queries once indexed."""
    resource = _bgzip_tsv(
        tmp_path / "alphamissense_hg38.tsv.gz",
        ["chr1\t100\tA\tG\t0.1", "chr1\t200\tC\tT\t0.9", "chr2\t150\tG\tA\t0.5"],
    )

    assert prepare_tabix_annotation(resource, "AlphaMissense") == resource
    assert _rows_at(resource, "chr1:200-200") == ["chr1\t200\tC\tT\t0.9"]
    assert _rows_at(resource, "chr2:1-1000") == ["chr2\t150\tG\tA\t0.5"]


@needs_tools("bgzip", "tabix")
def test_tabix_annotation_rebuilds_an_index_older_than_its_resource(tmp_path):
    """A resource replaced after indexing (a newer download) gets a new index;
    the old one would point into the old file."""
    resource = tmp_path / "phylop_hg38.tsv.gz"
    _bgzip_tsv(resource, [f"chr1\t{pos}\t0.5" for pos in range(1, 5000, 7)])
    run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", str(resource)])
    _bgzip_tsv(resource, ["chr1\t10\t2.0", "chr3\t20\t1.0"])
    os.utime(resource, (20, 20))
    os.utime(f"{resource}.tbi", (10, 10))

    prepare_tabix_annotation(str(resource), "PhyloP")
    assert _rows_at(str(resource), "chr1:10-10") == ["chr1\t10\t2.0"]
    assert _rows_at(str(resource), "chr3:20-20") == ["chr3\t20\t1.0"]


@pytest.mark.parametrize("build", ["hg19", "grch37", "hs38dh", "grch38", "chm13v2.0"])
def test_lowercase_build_names_are_accepted(build):
    args = build_parser()[0].parse_args(["qc", "fake-data", "--build", build])
    assert args.build == build


def _library(root: Path) -> ReferenceLibrary:
    """The hg38 library at ``root``, found from its genome FASTA."""
    fasta = root / "genomes" / "hg38.fa"
    fasta.parent.mkdir(parents=True)
    fasta.write_text(">chr1\nACGT\n")
    return ReferenceLibrary(str(fasta))


def test_annotation_resources_are_found_under_any_build_spelling(tmp_path):
    revel = tmp_path / "revel_GRCh38.tsv.gz"
    clinvar = tmp_path / "ref" / "clinvar_hs38.vcf.gz"
    clinvar.parent.mkdir()
    revel.touch()
    clinvar.touch()

    lib = _library(tmp_path)
    # Case-insensitive filesystems (Windows, macOS) may report another spelling.
    assert lib.revel_file and os.path.samefile(lib.revel_file, revel)
    assert lib.clinvar_vcf and os.path.samefile(lib.clinvar_vcf, clinvar)


def test_empty_resource_settings_discover_installed_library_data(monkeypatch, tmp_path):
    """Settings left empty in config.toml don't hide the library's own data."""
    ref = tmp_path / "ref"
    ref.mkdir()
    clinvar = ref / "clinvar_hg38.vcf.gz"
    pharmgkb = ref / "pharmgkb_hg38.tsv.gz"
    clinvar.touch()
    pharmgkb.touch()
    cache = tmp_path / "vep"
    (cache / "homo_sapiens" / "116_GRCh38").mkdir(parents=True)
    for name in ("clinvar_vcf_path", "pharmgkb_vcf_path", "vep_cache_directory"):
        monkeypatch.setitem(settings, name, "")

    lib = _library(tmp_path)
    assert lib.clinvar_vcf == str(clinvar)
    assert lib.pharmgkb_vcf == str(pharmgkb)
    assert lib.vep_cache == str(cache)
