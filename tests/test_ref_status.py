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


def test_ref_status_reports_every_annotation_vep_cache_and_genome(tmp_path, capsys):
    reflib = tmp_path / "reference"
    ref_dir = reflib / "ref"
    ref_dir.mkdir(parents=True)
    for name in (
        "gnomad_hg38.vcf.bgz", "clinvar_hg19.vcf.gz", "revel_hg38.tsv.gz",
        "gerp_hg38.bw",
    ):  # fmt: skip
        (ref_dir / name).write_text("x", encoding="utf-8")
    (ref_dir / "gnomad_hg38.vcf.bgz.tbi").write_text("x", encoding="utf-8")
    (ref_dir / "clinvar_hg19.vcf.gz.tbi").write_text("x", encoding="utf-8")
    (reflib / "vep" / "homo_sapiens" / "116_GRCh38").mkdir(parents=True)
    genomes = reflib / "genomes"
    genomes.mkdir()
    for name in ("hg38.fa.gz", "hg19.fa.gz"):
        (genomes / name).write_text(">chr1\nA\n", encoding="utf-8")
    for ext in (".fai", ".amb", ".ann", ".bwt", ".pac", ".sa"):
        (genomes / f"hg38.fa.gz{ext}").write_text("x", encoding="utf-8")

    args = Namespace(
        ref=str(reflib), genome_library=str(tmp_path / "g"), annotation_vcf="",
        input=None, json=False, values=False,
    )  # fmt: skip
    status = build_ref_status(args)

    annotations = status["annotations"]
    assert annotations["gnomad"]["hg38"]["installed"] is True
    assert annotations["clinvar"]["hg19"]["installed"] is True
    assert annotations["gerp"]["hg38"]["installed"] is True  # bigWig: self-indexed
    # Present but not tabix-indexed is not usable.
    assert annotations["revel"]["hg38"] == {
        "file": str(ref_dir / "revel_hg38.tsv.gz"),
        "installed": False,
    }
    assert annotations["phylop"]["hg19"] == {"file": "", "installed": False}
    assert "hg19" not in annotations["pharmgkb"]
    assert status["vepCache"]["versions"] == ["116_GRCh38"]
    by_name = {genome["name"]: genome for genome in status["genomes"]}
    assert by_name["hg38.fa.gz"]["bwaIndex"] is True
    assert by_name["hg19.fa.gz"]["faiIndex"] is False

    cmd_ref_status(args)
    printed = capsys.readouterr().out
    assert "gnomAD        missing       installed" in printed
    assert "REVEL         missing       not indexed" in printed
    assert "VEP cache: 116_GRCh38" in printed
