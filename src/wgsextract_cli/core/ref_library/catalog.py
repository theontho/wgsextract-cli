from __future__ import annotations

import csv
import logging
import os
import shutil
import zipfile
from typing import Any

from .downloads import (
    CancelEvent,
    ProgressCallback,
    download_file,
    verify_download_sha256,
)

_GENOME_DATA_CACHE: list[dict[str, Any]] = []




def load_genomes_from_csv(csv_path: str) -> list[dict[str, Any]]:
    if not os.path.exists(csv_path):
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
                        "md5": "",
                    }
                )
    except (OSError, csv.Error) as e:
        logging.error(f"Error reading {csv_path}: {e}")
    return genomes


def get_available_genomes() -> list[dict[str, Any]]:
    global _GENOME_DATA_CACHE
    if _GENOME_DATA_CACHE:
        return _GENOME_DATA_CACHE

    cli_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    csv_path = os.path.join(cli_root, "assets/reference/seed_genomes.csv")

    if os.path.exists(csv_path):
        _GENOME_DATA_CACHE = load_genomes_from_csv(csv_path)

    # Hardcoded fallback and MD5 source
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
            "md5": "7cee777f1939f4028926017158ed5512",
        },
        {
            "code": "hs37d5",
            "source": "NIH",
            "final": "hs37d5.fa.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/1000genomes/ftp/technical/reference/phase2_reference_assembly_sequence/hs37d5.fa.gz",
            "label": "hs37d5 (Dante) (NIH) (Rec)",
            "description": "hs37d5 (1K Genome; @US NIH)",
            "md5": "5a23f5a85bd78221010561466907bf7d",
        },
    ]

    if not _GENOME_DATA_CACHE:
        _GENOME_DATA_CACHE = core_genomes
    else:
        # Merge MD5s from core into CSV-loaded data
        for cg in core_genomes:
            for g in _GENOME_DATA_CACHE:
                if g["code"] == cg["code"] and not g.get("md5"):
                    g["md5"] = cg["md5"]

    pet_genomes = [
        {
            "code": "UU_Cfam_GSD_1.0",
            "source": "NCBI",
            "final": "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/genomes/refseq/vertebrate_mammalian/Canis_lupus_familiaris/latest_assembly_versions/GCF_011100685.1_UU_Cfam_GSD_1.0/GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz",
            "label": "Dog (Canis lupus familiaris) UU_Cfam_GSD_1.0 (NCBI)",
            "description": "Dog reference genome (Canis lupus familiaris; @NCBI UU_Cfam_GSD_1.0)",
            "md5": "a6f017498a4fa2ec9efa0a4f12ccb42c",
        },
        {
            "code": "Fca126_mat1.0",
            "source": "NCBI",
            "final": "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz",
            "url": "https://ftp.ncbi.nlm.nih.gov/genomes/refseq/vertebrate_mammalian/Felis_catus/latest_assembly_versions/GCF_018350175.1_F.catus_Fca126_mat1.0/GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz",
            "label": "Cat (Felis catus) Fca126_mat1.0 (NCBI)",
            "description": "Cat reference genome (Felis catus; @NCBI Fca126_mat1.0)",
            "md5": "d271b9b06c5270d2d662534df8a4fcd9",
        },
    ]

    # Append pet genomes if not already present
    for pg in pet_genomes:
        if not any(g["code"] == pg["code"] for g in _GENOME_DATA_CACHE):
            _GENOME_DATA_CACHE.append(pg)

    return _GENOME_DATA_CACHE


GENOME_DATA = get_available_genomes()


def get_grouped_genomes() -> list[dict[str, Any]]:
    all_data = get_available_genomes()
    grouped = {}
    for item in all_data:
        fname = item["final"]
        if fname not in grouped:
            grouped[fname] = {
                "final": fname,
                "label": item["label"],
                "description": item.get("description", ""),
                "code": item.get("code", ""),
                "sources": [],
            }
        grouped[fname]["sources"].append(item)
    return list(grouped.values())


def get_genome_status(final_name: str, reflib_dir: str) -> str:
    """Returns 'installed', 'unindexed', 'incomplete', or 'missing'."""
    if not reflib_dir:
        return "missing"
    target_dir = os.path.join(reflib_dir, "genomes")
    final_path = os.path.join(target_dir, final_name)
    partial_path = final_path + ".partial"
    fai_path = final_path + ".fai"

    if os.path.exists(final_path):
        if os.path.exists(fai_path):
            return "installed"
        else:
            return "unindexed"
    if os.path.exists(partial_path):
        return "incomplete"
    return "missing"


def is_genome_installed(final_name: str, reflib_dir: str) -> bool:
    return get_genome_status(final_name, reflib_dir) == "installed"


def install_standard_mappability_maps(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Install standard Delly CNV mappability maps into the reference library."""
    from wgsextract_cli.core.constants import DELLY_MAPPABILITY_MAPS

    maps_dir = os.path.join(reflib_dir, "maps")
    os.makedirs(maps_dir, exist_ok=True)
    ok = True
    for build, entry in DELLY_MAPPABILITY_MAPS.items():
        if cancel_event and cancel_event.is_set():
            return False
        filename = entry["filename"]
        dest_path = os.path.join(maps_dir, filename)
        if not os.path.exists(dest_path):
            logging.info("Downloading Delly %s mappability map...", build)
            ok = (
                download_file(entry["url"], dest_path, progress_callback, cancel_event)
                and ok
            )
        if cancel_event and cancel_event.is_set():
            return False
        for suffix, url in entry.get("sidecars", {}).items():
            sidecar_path = dest_path + suffix
            if os.path.exists(sidecar_path):
                continue
            ok = (
                download_file(url, sidecar_path, progress_callback, cancel_event) and ok
            )
            if cancel_event and cancel_event.is_set():
                return False
    if not ok:
        logging.warning("One or more Delly mappability map downloads failed.")
    return ok


def install_mappability_maps(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and extracts the mirrored Delly mappability map set."""
    from wgsextract_cli.core.constants import (
        MAPPABILITY_MAP_ARCHIVE_FILENAME,
        MAPPABILITY_MAP_ARCHIVE_SHA256,
        MAPPABILITY_MAP_ARCHIVE_URL,
        MAPPABILITY_MAP_FILES,
    )

    maps_dir = os.path.join(reflib_dir, "maps")
    os.makedirs(maps_dir, exist_ok=True)
    if all(
        os.path.isfile(os.path.join(maps_dir, name)) for name in MAPPABILITY_MAP_FILES
    ):
        logging.info("Delly mappability maps are already installed.")
        return True

    archive_path = os.path.join(reflib_dir, MAPPABILITY_MAP_ARCHIVE_FILENAME)
    try:
        logging.info(
            "Downloading Delly mappability maps from %s...",
            MAPPABILITY_MAP_ARCHIVE_URL,
        )
        if not download_file(
            MAPPABILITY_MAP_ARCHIVE_URL,
            archive_path,
            progress_callback,
            cancel_event,
        ):
            return False
        if not verify_download_sha256(archive_path, MAPPABILITY_MAP_ARCHIVE_SHA256):
            return False
        if cancel_event and cancel_event.is_set():
            logging.info("Mappability map installation cancelled by user.")
            return False

        logging.info("Extracting Delly mappability maps to %s...", maps_dir)
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            for file_name in MAPPABILITY_MAP_FILES:
                member = f"maps/{file_name}"
                if member not in names:
                    logging.error("Mappability map archive is missing %s.", member)
                    return False
                target = os.path.join(maps_dir, file_name)
                with archive.open(member) as source, open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)

        logging.info("Delly mappability maps are installed.")
        return True
    except (OSError, zipfile.BadZipFile) as e:
        logging.error("Failed to install Delly mappability maps: %s", e)
        return False
    finally:
        try:
            if os.path.exists(archive_path):
                os.remove(archive_path)
        except OSError:
            pass
