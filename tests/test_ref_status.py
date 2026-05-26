import json
from argparse import Namespace

from wgsextract_cli.commands.ref.status import build_ref_status, cmd_ref_status
from wgsextract_cli.core.config import settings


def test_ref_status_reports_gui_library_values(tmp_path, capsys):
    reflib = tmp_path / "reference"
    ref_dir = reflib / "ref"
    maps_dir = reflib / "maps"
    genome_library = tmp_path / "genomes"
    test_genome = genome_library / "wgsextract-benchmark-hg19-mini"
    ref_dir.mkdir(parents=True)
    maps_dir.mkdir()
    test_genome.mkdir(parents=True)

    (ref_dir / "genes_hg19.tsv").write_text("gene\n", encoding="utf-8")
    (ref_dir / "genes_hg38.tsv").write_text("gene\n", encoding="utf-8")
    (ref_dir / "All_SNPs_hg19_ref.tab.gz").write_text("snps\n", encoding="utf-8")
    (ref_dir / "spliceai_hg19.vcf.gz").write_text("spliceai\n", encoding="utf-8")
    (ref_dir / "alphamissense_hg19.tsv.gz").write_text("am\n", encoding="utf-8")
    (ref_dir / "pharmgkb_hg19.vcf.gz").write_text("pgkb\n", encoding="utf-8")
    (test_genome / "genome-config.toml").write_text("id = 'test'\n", encoding="utf-8")

    cmd_ref_status(
        Namespace(
            ref=str(reflib),
            genome_library=str(genome_library),
            annotation_vcf="",
            input=str(tmp_path / "sample.hg19.vcf.gz"),
            json=False,
            values=True,
        )
    )

    output = json.loads(capsys.readouterr().out)
    values = output["values"]
    assert values["library.geneMapInstalled"] == "true"
    assert values["library.isBootstrapped"] == "true"
    assert values["library.annotationVcfInstalled"] == "true"
    assert values["library.annotationVcfReady"] == "true"
    assert values["library.spliceaiInstalled"] == "true"
    assert values["library.alphamissenseInstalled"] == "true"
    assert values["library.pharmgkbInstalled"] == "true"
    assert values["library.testGenomeInstalled"] == "true"
    assert values["library.testGenomeStatus"] == "installed"


def test_ref_status_uses_custom_annotation_vcf_for_readiness(tmp_path):
    reflib = tmp_path / "reference"
    reflib.mkdir()
    custom_vcf = tmp_path / "custom.vcf.gz"
    custom_vcf.write_text("vcf\n", encoding="utf-8")

    status = build_ref_status(
        Namespace(
            ref=str(reflib),
            genome_library=str(tmp_path / "genomes"),
            annotation_vcf=str(custom_vcf),
            input=None,
        )
    )

    assert status["annotationVcf"]["installed"] is False
    assert status["annotationVcf"]["ready"] is True
    assert status["annotationVcf"]["argument"] == str(custom_vcf)


def test_ref_status_prefers_configured_library_over_default_ref_fasta(tmp_path):
    inferred_reflib = tmp_path / "reference"
    configured_reflib = tmp_path / "configured-reference"
    genomes = inferred_reflib / "genomes"
    fasta = genomes / "hg38.fa"
    genomes.mkdir(parents=True)
    configured_reflib.mkdir()
    fasta.write_text(">chr1\nA\n", encoding="utf-8")
    old_reflib = settings.get("reference_library")
    try:
        settings["reference_library"] = str(configured_reflib)
        status = build_ref_status(
            Namespace(
                ref=str(fasta),
                genome_library=str(tmp_path / "genomes"),
                annotation_vcf="",
                input=None,
                _explicit_dests=set(),
            )
        )
    finally:
        if old_reflib is None:
            settings.pop("reference_library", None)
        else:
            settings["reference_library"] = old_reflib

    assert status["referenceLibrary"]["path"] == str(configured_reflib)


def test_ref_status_infers_library_from_reference_fasta(tmp_path):
    reflib = tmp_path / "reference"
    genomes = reflib / "genomes"
    fasta = genomes / "hg38.fa"
    genomes.mkdir(parents=True)
    fasta.write_text(">chr1\nA\n", encoding="utf-8")
    old_reflib = settings.get("reference_library")
    try:
        settings.pop("reference_library", None)
        status = build_ref_status(
            Namespace(
                ref=str(fasta),
                genome_library=str(tmp_path / "genomes"),
                annotation_vcf="",
                input=None,
            )
        )
    finally:
        if old_reflib is None:
            settings.pop("reference_library", None)
        else:
            settings["reference_library"] = old_reflib

    assert status["referenceLibrary"]["path"] == str(reflib)
