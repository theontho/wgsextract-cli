import pytest

from wgsextract_cli.commands.align import _require_unambiguous_library
from wgsextract_cli.core.utils import WGSExtractError


def _library(tmp_path, *names):
    genomes = tmp_path / "genomes"
    genomes.mkdir()
    for name in names:
        (genomes / name).write_text(">chr1\nACGT\n")
        (genomes / f"{name}.fai").write_text("chr1\t4\t6\t4\t5\n")
    return tmp_path


def test_library_with_several_genomes_needs_a_fasta(tmp_path):
    library = _library(tmp_path, "hg19_WGSE.fa.gz", "hg38.fa.gz")
    with pytest.raises(WGSExtractError, match="pass --ref <FASTA>") as error:
        _require_unambiguous_library(str(library), "BWA alignment")
    assert "hg19_WGSE.fa.gz" in str(error.value) and "hg38.fa.gz" in str(error.value)


def test_library_with_one_genome_is_unambiguous(tmp_path):
    _require_unambiguous_library(str(_library(tmp_path, "hg38.fa.gz")), "BWA alignment")


def test_explicit_fasta_is_used_as_given(tmp_path):
    library = _library(tmp_path, "hg19_WGSE.fa.gz", "hg38.fa.gz")
    _require_unambiguous_library(str(library / "genomes" / "hg38.fa.gz"), "BWA")
