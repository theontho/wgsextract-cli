from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from wgsextract_cli.core.paths import absolute_path

_GENOME_DATA_CACHE: list[dict[str, Any]] = []


def load_genomes_from_csv(csv_path: str) -> list[dict[str, Any]]:
    if not Path(csv_path).exists():
        return []
    genomes = []
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                genomes.append(
                    {
                        "code": row.get("Pyth Code"),
                        "source": row.get("Source"),
                        "final": row.get("Final File Name"),
                        "url": row.get("URL"),
                        "label": row.get("Library Menu Label"),
                        "description": row.get("Description", ""),
                        "md5": row.get("Signature") or "",
                    }
                )
    except (OSError, csv.Error) as e:
        logging.error(f"Error reading {csv_path}: {e}")
    return genomes


def get_available_genomes() -> list[dict[str, Any]]:
    global _GENOME_DATA_CACHE
    if _GENOME_DATA_CACHE:
        return _GENOME_DATA_CACHE

    cli_root = absolute_path(__file__).parents[2]
    csv_path = cli_root / "assets" / "reference" / "seed_genomes.csv"

    if Path(csv_path).exists():
        _GENOME_DATA_CACHE = load_genomes_from_csv(str(csv_path))

    # Fallback when the catalog is missing. "md5" is the WGS Extract sequence
    # dictionary signature (MD5 of the sorted SN/LN pairs, as calculate_bam_md5
    # computes for a BAM header), not a checksum of the file bytes; the catalog
    # has it in its Signature column.
    core_genomes = [
        {
            "code": "hg38",
            "source": "NIH",
            "final": "hg38.fa.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/genomes/archive/old_genbank/Eukaryotes/vertebrates_mammals/Homo_sapiens/GRCh38/seqs_for_alignment_pipelines/GCA_000001405.15_GRCh38_no_alt_analysis_set.fna.gz",
            "label": "hg38 (Nebula) (NIH) (Rec)",
            "description": "hg38 No Alt (1K Genome; @US NIH)",
            "md5": "bd894134bddba260df88a90123a2ee9c",
        },
        {
            "code": "hg19",
            "source": "NIH",
            "final": "hg19.fa.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/1000genomes/ftp/technical/reference/phase2_reference_assembly_sequence/hg19.fa.gz",
            "label": "hg19 (Yoruba) (NIH) (Rec)",
            "description": "hg19 (1K Genome; @US NIH)",
            "md5": "ee4efe40ebd6f9468dab89963dcc5b65",
        },
        {
            "code": "T2Tv20",
            "source": "AWS",
            "final": "chm13v2.0.fa.gz",
            "url": "https://s3-us-west-2.amazonaws.com/human-pangenomics/T2T/CHM13/assemblies/analysis_set/chm13v2.0.fa.gz",
            "label": "T2T_v2.0 (PGP/HPP chrN) (Rec)",
            "description": "T2T v2.0 (chm13 v1.1; HG002 Y v2.7; UCSC SN; @AWS)",
            "md5": "ed9f80c8e04c461cba35dd1b85a2e2e8",
        },
        {
            "code": "hs37d5",
            "source": "NIH",
            "final": "hs37d5.fa.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/1000genomes/ftp/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz",
            "label": "hs37d5 (Dante) (NIH) (Rec)",
            "description": "hs37d5 (1K Genome; @US NIH)",
            "md5": "92c90b84dac3d9d8402bf2f2dbe95961",
        },
    ]

    if not _GENOME_DATA_CACHE:
        _GENOME_DATA_CACHE = core_genomes

    pet_genomes = [
        {
            "code": "UU_Cfam_GSD_1.0",
            "source": "NCBI",
            "final": "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/genomes/refseq/vertebrate_mammalian/Canis_lupus_familiaris/latest_assembly_versions/GCF_011100685.1_UU_Cfam_GSD_1.0/GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz",
            "label": "Dog (Canis lupus familiaris) UU_Cfam_GSD_1.0 (NCBI)",
            "description": "Dog reference genome (Canis lupus familiaris; @NCBI UU_Cfam_GSD_1.0)",
            "md5": "567b0b7efc021c69f64fe63d85dbc797",
        },
        {
            "code": "Fca126_mat1.0",
            "source": "NCBI",
            "final": "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/genomes/refseq/vertebrate_mammalian/Felis_catus/latest_assembly_versions/GCF_018350175.1_F.catus_Fca126_mat1.0/GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz",
            "label": "Cat (Felis catus) Fca126_mat1.0 (NCBI)",
            "description": "Cat reference genome (Felis catus; @NCBI Fca126_mat1.0)",
            "md5": "492b4726909f048464fdd00232f63f47",
        },
    ]

    # Append pet genomes if not already present
    for pg in pet_genomes:
        if not any(g["final"] == pg["final"] for g in _GENOME_DATA_CACHE):
            _GENOME_DATA_CACHE.append(pg)

    return _GENOME_DATA_CACHE


def get_genome_status(final_name: str, reflib_dir: str) -> str:
    """Returns 'installed', 'unindexed', 'incomplete', or 'missing'."""
    if not reflib_dir:
        return "missing"
    target_dir = str(Path(reflib_dir, "genomes"))
    final_path = str(Path(target_dir, final_name))
    partial_path = final_path + ".partial"
    fai_path = final_path + ".fai"

    if Path(final_path).exists():
        if Path(fai_path).exists():
            return "installed"
        else:
            return "unindexed"
    if Path(partial_path).exists():
        return "incomplete"
    return "missing"
