import pytest

from tests import test_microarray_indels as indel_fixtures
from tests.htslib_helpers import needs_tools, run_wgsextract
from wgsextract_cli.commands import microarray
from wgsextract_cli.core.utils import run_command

alignment = indel_fixtures.alignment
probe_inputs = indel_fixtures.probe_inputs


@needs_tools("bcftools", "samtools", "bgzip", "tabix")
@pytest.mark.parametrize("kind", ["vcf", "bcf"])
def test_cli_prepares_unindexed_sources_without_changing_originals(
    tmp_path, monkeypatch, probe_inputs, kind
):
    manifest, lib = probe_inputs
    monkeypatch.setattr(microarray, "ReferenceLibrary", lambda *a, **k: lib)
    monkeypatch.setattr(
        microarray, "_template_search_roots", lambda *a: [str(tmp_path)]
    )
    plain_target = tmp_path / "targets.tsv"
    plain_target.write_text("chr1\t700\tC\n")
    target = tmp_path / "targets.tsv.gz"
    with target.open("wb") as stream:
        run_command(["bgzip", "-c", str(plain_target)], stdout=stream)
    run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", str(target)])
    source = tmp_path / "input.vcf"
    source.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=1,length=1000>\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample\n"
        "1\t700\t.\tCGT\tC\t.\tPASS\t.\tGT\t0/1\n"
    )
    if kind == "bcf":
        binary = source.with_suffix(".bcf")
        run_command(["bcftools", "view", "-Ob", "-o", str(binary), str(source)])
        source = binary
    original = source.read_bytes()
    out = tmp_path / "out"
    run_wgsextract(
        "microarray",
        "--input",
        str(source),
        "--ref",
        lib.fasta,
        "--ref-vcf-tab",
        str(target),
        "--threads",
        "1",
        "--region",
        "chr1:650-780",
        "--formats",
        "23andme_v5_2025",
        "--indel-manifest",
        "23andme_v5_2025=" + manifest,
        "--outdir",
        str(out),
    )
    exported = next(out.glob("*23andMe_V5_2025.txt")).read_text()
    assert exported.rstrip().endswith("rsProbe\t1\t700\tDI")
    assert source.read_bytes() == original
    assert not source.with_name(source.name + ".gz").exists()
    assert not source.with_name(source.name + ".csi").exists()
    assert list(out.glob("IndelInput_*/input.vcf.gz.tbi"))
