import logging
import os

from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.ref_library import download_file
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import calculate_bsd_sum


def cmd_vep_download(args):
    verify_dependencies(["curl", "tar"])

    vep_version = args.vep_version
    species = args.species
    assembly = args.assembly
    mirror = args.mirror

    mirror_hosts = {
        "us-east": "useast.ensembl.org",
        "uk": "ftp.ensembl.org",
        "asia": "asia.ensembl.org",
        "aws": "annotation-cache",
    }
    host = mirror_hosts.get(mirror, "useast.ensembl.org")

    cache_root = args.vep_cache
    if not cache_root:
        lib = ReferenceLibrary(args.ref)
        if lib.root and os.path.isdir(lib.root):
            cache_root = os.path.join(lib.root, "vep")
        else:
            cache_root = os.path.expanduser("~/.vep")

    os.makedirs(cache_root, exist_ok=True)

    filename = f"{species}_vep_{vep_version}_{assembly}.tar.gz"
    url = f"https://{host}/pub/release-{vep_version}/variation/indexed_vep_cache/{filename}"
    target_path = os.path.join(cache_root, filename)

    progress_callback = getattr(args, "progress_callback", None)
    cancel_event = getattr(args, "cancel_event", None)

    logging.info(LOG_MESSAGES["vep_downloading"].format(host=host))
    success = download_file(url, target_path, progress_callback, cancel_event)
    if not success:
        if cancel_event and cancel_event.is_set():
            logging.info("Download cancelled.")
        else:
            logging.error("Download failed.")
        return False

    try:
        checksum_url = f"https://{host}/pub/release-{vep_version}/variation/indexed_vep_cache/CHECKSUMS"
        checksum_path = target_path + ".CHECKSUMS"
        logging.info(LOG_MESSAGES["vep_verifying_checksums"])

        try:
            run_command(["curl", "-s", "-L", "-o", checksum_path, checksum_url])
            found_sum = None
            found_blocks = None
            with open(checksum_path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4 and parts[-1] == filename:
                        found_sum = int(parts[0])
                        found_blocks = int(parts[1])
                        break

            if found_sum is not None:
                local_sum, local_blocks = calculate_bsd_sum(target_path)
                if local_sum == found_sum and local_blocks == found_blocks:
                    logging.info(
                        LOG_MESSAGES["vep_checksum_ok"].format(
                            sum=local_sum, blocks=local_blocks
                        )
                    )
                else:
                    logging.warning(
                        LOG_MESSAGES["vep_checksum_failed"].format(
                            expected_sum=found_sum,
                            expected_blocks=found_blocks,
                            actual_sum=local_sum,
                            actual_blocks=local_blocks,
                        )
                    )
        except Exception as e:
            logging.debug(f"Checksum verification failed: {e}")
        finally:
            if os.path.exists(checksum_path):
                os.remove(checksum_path)

        if cancel_event and cancel_event.is_set():
            logging.info("Download cancelled before extraction.")
            return False

        logging.info(LOG_MESSAGES["vep_extracting"].format(filename=filename))
        run_command(["tar", "-xzf", target_path, "-C", cache_root])
        logging.info(LOG_MESSAGES["vep_extraction_complete"])
        logging.info(LOG_MESSAGES["vep_ready"].format(path=f"{cache_root}/{species}"))
        os.remove(target_path)
        return True
    except Exception as e:
        logging.error(f"Post-download processing failed: {e}")
        return False


def cmd_vep_verify(args):
    vep_version = args.vep_version
    species = args.species
    assembly = args.assembly

    cache_root = args.vep_cache
    if not cache_root:
        lib = ReferenceLibrary(args.ref)
        if lib.root and os.path.isdir(lib.root):
            cache_root = os.path.join(lib.root, "vep")
        else:
            cache_root = os.path.expanduser("~/.vep")

    species_dir = os.path.join(cache_root, species)
    version_dir = os.path.join(species_dir, f"{vep_version}_{assembly}")

    logging.info(
        LOG_MESSAGES["vep_verifying"].format(
            species=species, version=vep_version, assembly=assembly
        )
    )
    logging.info(LOG_MESSAGES["vep_location"].format(path=version_dir))

    if not os.path.exists(version_dir):
        msg = LOG_MESSAGES["vep_cache_missing"].format(path=version_dir)
        logging.error(msg)
        raise WGSExtractError(msg)

    # Check for basic files
    info_file = os.path.join(version_dir, "info.txt")
    if os.path.exists(info_file):
        logging.info(LOG_MESSAGES["vep_info_found"])
    else:
        logging.warning(LOG_MESSAGES["vep_info_missing"])

    # Check for chromosomal directories
    missing_chrs = []
    for c in list(range(1, 23)) + ["X", "Y", "MT"]:
        chr_dir = os.path.join(version_dir, str(c))
        if not os.path.exists(chr_dir):
            missing_chrs.append(str(c))

    if missing_chrs:
        logging.warning(
            LOG_MESSAGES["vep_chrs_missing"].format(chrs=", ".join(missing_chrs))
        )
    else:
        logging.info(LOG_MESSAGES["vep_chrs_ok"])

    logging.info(LOG_MESSAGES["vep_verification_complete"])
    return True


def preprocess_vcf_chr_prefix(input_path, output_path):
    """
    Adds 'chr' prefix to numeric chromosomes if missing.
    Equivalent to the sed command in run_vep_batch.py.
    """
    import gzip

    logging.info(
        LOG_MESSAGES["vep_preprocessing_chr"].format(
            input=input_path, output=output_path
        )
    )

    open_func = gzip.open if input_path.endswith(".gz") else open
    with open_func(input_path, "rt") as f_in, open(output_path, "w") as f_out:
        for line in f_in:
            if line.startswith("##contig=<ID="):
                # Replace ID=1 with ID=chr1, but avoid ID=chrchr1
                if "ID=chr" not in line:
                    line = line.replace("ID=", "ID=chr")
            elif line.startswith("#"):
                pass
            else:
                # Variant line
                if not line.startswith("chr"):
                    parts = line.split("\t", 1)
                    chrom = parts[0]
                    # Only prefix if it's a standard chromosome name
                    if chrom.isdigit() or chrom in ["X", "Y", "MT", "M"]:
                        new_chrom = f"chr{chrom}"
                        if new_chrom == "chrMT":
                            new_chrom = "chrM"
                        line = f"{new_chrom}\t{parts[1]}"
            f_out.write(line)
