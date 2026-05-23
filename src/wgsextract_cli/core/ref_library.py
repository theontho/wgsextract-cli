from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from wgsextract_cli.core.dev_download_cache import (
    drop_cached_download,
    restore_cached_download,
    store_download_in_dev_cache,
)
from wgsextract_cli.core.download_progress import (
    DownloadCancelled,
    copy_response_to_file,
    curl_progress_args,
)
from wgsextract_cli.core.utils import run_command

_GENOME_DATA_CACHE: list[dict[str, Any]] = []


def resolve_github_release_asset_sha256(url: str) -> str | None:
    """Return GitHub's sha256 digest for a release asset URL, if applicable."""
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None

    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) == 6 and parts[2:4] == ["releases", "download"]:
        owner, repo, tag, asset_name = parts[0], parts[1], parts[4], parts[5]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    elif len(parts) == 6 and parts[2:5] == ["releases", "latest", "download"]:
        owner, repo, asset_name = parts[0], parts[1], parts[5]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    else:
        return None

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "wgsextract-cli",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    req = Request(api_url, headers=headers)
    with urlopen(req, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))

    for asset in release.get("assets", []):
        if asset.get("name") != asset_name:
            continue
        digest = str(asset.get("digest", ""))
        match = re.fullmatch(r"(?i)sha256:([a-f0-9]{64})", digest)
        if not match:
            raise ValueError(
                f"GitHub release asset {asset_name} did not include a sha256 digest."
            )
        return match.group(1).lower()

    raise ValueError(
        f"GitHub release asset metadata was not found for {asset_name} at {api_url}."
    )


def verify_download_sha256(path: str, expected_sha256: str | None) -> bool:
    """Verify a downloaded file against an expected SHA-256 digest."""
    if not expected_sha256:
        return True

    sha256 = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
    except OSError as e:
        logging.error(f"Could not read downloaded file for checksum verification: {e}")
        return False

    actual_sha256 = sha256.hexdigest()
    if actual_sha256 == expected_sha256.lower():
        logging.info(f"Verified GitHub release asset SHA256: {actual_sha256}")
        return True

    logging.error("Downloaded file checksum mismatch.")
    logging.error(f"Expected SHA256: {expected_sha256.lower()}")
    logging.error(f"Actual SHA256:   {actual_sha256}")
    try:
        os.remove(path)
    except OSError:
        pass
    return False


def download_file(
    url: str,
    dest: str,
    progress_callback: Callable[[int, int, float], None] | None = None,
    cancel_event: Any | None = None,
) -> bool:
    """Downloads a file with progress reporting, optional cancellation, and resume support."""
    partial_dest = dest + ".partial"
    try:
        expected_sha256 = resolve_github_release_asset_sha256(url)
    except OSError as e:
        logging.warning(
            "Could not resolve GitHub release asset checksum for %s: %s. "
            "Continuing without GitHub asset SHA-256 verification.",
            url,
            e,
        )
        expected_sha256 = None
    except ValueError as e:
        logging.error(
            "Could not resolve GitHub release asset checksum for %s: %s", url, e
        )
        return False

    checksum_hint = f"sha256:{expected_sha256}" if expected_sha256 else None
    dest_path = Path(dest)
    if not os.path.exists(dest) and not os.path.exists(partial_dest):
        if restore_cached_download(url, dest_path, checksum_hint=checksum_hint):
            if verify_download_sha256(dest, expected_sha256):
                return True
            drop_cached_download(url, dest_path, checksum_hint=checksum_hint)

    # Use curl only when it can surface its native progress bar directly.
    # Non-TTY runs should take the urllib path so progress is emitted as logs.
    curl_args = curl_progress_args()
    if (
        progress_callback is None
        and cancel_event is None
        and "--progress-bar" in curl_args
    ):
        try:
            # Use -L to follow redirects, -C - for resume
            cmd = ["curl", "-L", *curl_args]
            if os.path.exists(dest):
                cmd.extend(["-C", "-"])
            cmd.extend(["-o", dest, url])

            run_command(cmd, capture_output=False)
            verified = verify_download_sha256(dest, expected_sha256)
            if verified:
                store_download_in_dev_cache(url, dest_path, checksum_hint=checksum_hint)
            return verified
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("curl download failed, falling back to urllib: %s", e)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    initial_size = 0
    mode: Literal["ab", "wb"] = "wb"

    # If the final file exists but we are here, it might be incomplete (e.g. missing index)
    # Move it to .partial to attempt a resume/verify
    if not os.path.exists(partial_dest) and os.path.exists(dest):
        os.rename(dest, partial_dest)

    if os.path.exists(partial_dest):
        initial_size = os.path.getsize(partial_dest)
        if initial_size > 0:
            headers["Range"] = f"bytes={initial_size}-"
            mode = "ab"
        else:
            mode = "wb"

    try:
        req = Request(url, headers=headers)
        with urlopen(req) as response:
            code = response.getcode()
            # If we requested a range but got 200, the server doesn't support range or sent full file
            if initial_size > 0 and code == 200:
                logging.info(
                    "Server does not support Range requests, starting from scratch."
                )
                initial_size = 0
                mode = "wb"

            def write_partial(f: BinaryIO) -> None:
                copy_response_to_file(
                    response,
                    f,
                    initial_size=initial_size,
                    progress_callback=progress_callback,
                    progress_label=os.path.basename(dest),
                    cancel_event=cancel_event,
                )

            with open(partial_dest, mode) as f:
                write_partial(f)

        # Rename to final destination on success
        if os.path.exists(dest):
            os.remove(dest)
        os.rename(partial_dest, dest)
        verified = verify_download_sha256(dest, expected_sha256)
        if verified:
            store_download_in_dev_cache(url, dest_path, checksum_hint=checksum_hint)
        return verified
    except DownloadCancelled as e:
        logging.info(str(e))
    except Exception as e:
        logging.error(f"Download error: {e}")
    return False


def load_genomes_from_csv(csv_path):
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
    except Exception as e:
        logging.error(f"Error reading {csv_path}: {e}")
    return genomes


def get_available_genomes():
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


def get_grouped_genomes():
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
    cancel_event: Any | None = None,
    progress_callback: Callable[[int, int, float], None] | None = None,
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
    cancel_event: Any | None = None,
    progress_callback: Callable[[int, int, float], None] | None = None,
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
