import argparse
import logging
import shutil
import subprocess
from pathlib import Path

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import expand_path
from wgsextract_cli.core.ref_library import download_file
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)

# Ensembl's useast/asia web mirrors do not serve /pub (HTTP 403).
VEP_MIRRORS = {
    "uk": "https://ftp.ensembl.org/pub",
    "ebi": "https://ftp.ebi.ac.uk/ensemblorg/pub",
}


def _python_bsd_sum(file_path: str) -> int:
    checksum = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(1 << 20):
            for byte in chunk:
                checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + byte) & 0xFFFF
    return checksum


def _native_bsd_sum(file_path: str) -> tuple[int, int] | None:
    """BSD sum via the system tool (macOS `cksum -o 1`, GNU `sum -r`), if present."""
    for cmd in (["cksum", "-o", "1"], ["sum", "-r"]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            res = run_command([*cmd, file_path], capture_output=True, check=False)
            if res.returncode == 0:
                fields = res.stdout.split()
                return int(fields[0]), int(fields[1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
            logging.debug(f"{cmd[0]} BSD checksum failed: {e}")
    return None


def calculate_bsd_sum(file_path: str) -> tuple[int, int]:
    """
    BSD `sum` checksum and 1 KiB block count, as listed in Ensembl CHECKSUMS.

    The checksum is an inherently sequential rotate-and-add over every byte, so
    the native tool (~GB/s) is preferred; pure Python manages ~10 MB/s.
    """
    native = _native_bsd_sum(file_path)
    if native is not None:
        return native
    size = Path(file_path).stat().st_size
    if size > 512 * 1024 * 1024:
        logging.warning(
            "No native BSD sum tool found; computing the checksum in Python. "
            "This can take a long time for large files."
        )
    return _python_bsd_sum(file_path), -(-size // 1024)


def _expected_bsd_sum(checksums_path: str, filename: str) -> tuple[int, int] | None:
    """Find `<sum> <blocks> <filename>` in an Ensembl CHECKSUMS file."""
    with open(checksums_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[-1] == filename:
                return int(parts[0]), int(parts[1])
    return None


def _resolve_vep_cache_root(args: argparse.Namespace) -> str:
    # The same order as vep run: an explicit cache, the configured one, then
    # the reference library's, then VEP's default.
    cache_root = getattr(args, "vep_cache", None) or settings.get("vep_cache_directory")
    if cache_root:
        return str(cache_root)
    lib = ReferenceLibrary(getattr(args, "ref", None))
    if lib.root and Path(lib.root).is_dir():
        return str(Path(lib.root, "vep"))
    return expand_path("~/.vep")


def _verify_vep_tarball(base_url: str, target_path: str, filename: str) -> None:
    checksum_url = f"{base_url}/CHECKSUMS"
    checksum_path = target_path + ".CHECKSUMS"
    logging.info(LOG_MESSAGES["vep_verifying_checksums"])
    try:
        run_command(
            ["curl", "--fail", "-sS", "-L", "-o", checksum_path, checksum_url],
            capture_output=True,
        )
        expected = _expected_bsd_sum(checksum_path, filename)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logging.warning(f"Checksum verification could not be completed: {e}")
        return
    finally:
        if Path(checksum_path).exists():
            Path(checksum_path).unlink()

    if expected is None:
        logging.warning(f"{filename} is not listed in {checksum_url}; not verified.")
        return
    actual = calculate_bsd_sum(target_path)
    if actual != expected:
        # Drop the corrupt tarball so the next attempt downloads it afresh.
        Path(target_path).unlink()
        raise WGSExtractError(
            LOG_MESSAGES["vep_checksum_failed"].format(
                expected_sum=expected[0],
                expected_blocks=expected[1],
                actual_sum=actual[0],
                actual_blocks=actual[1],
            )
        )
    logging.info(
        LOG_MESSAGES["vep_checksum_ok"].format(sum=actual[0], blocks=actual[1])
    )


def cmd_vep_download(args: argparse.Namespace) -> None:
    verify_dependencies(["curl", "tar"])

    species = args.species
    vep_version = args.vep_version
    base_url = (
        f"{VEP_MIRRORS[args.mirror]}/release-{vep_version}/variation/indexed_vep_cache"
    )
    cache_root = _resolve_vep_cache_root(args)
    Path(cache_root).mkdir(parents=True, exist_ok=True)

    filename = f"{species}_vep_{vep_version}_{args.assembly}.tar.gz"
    target_path = str(Path(cache_root, filename))
    cancel_event = getattr(args, "cancel_event", None)

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    logging.info(LOG_MESSAGES["vep_downloading"].format(host=base_url))
    if not download_file(
        f"{base_url}/{filename}",
        target_path,
        getattr(args, "progress_callback", None),
        cancel_event,
    ):
        raise WGSExtractError(
            "VEP cache download cancelled."
            if cancelled()
            else f"VEP cache download failed: {base_url}/{filename}"
        )

    _verify_vep_tarball(base_url, target_path, filename)
    if cancelled():
        raise WGSExtractError("VEP cache download cancelled before extraction.")

    logging.info(LOG_MESSAGES["vep_extracting"].format(filename=filename))
    try:
        run_command(["tar", "-xzf", target_path, "-C", cache_root])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Failed to extract {target_path}: {e}") from e
    logging.info(LOG_MESSAGES["vep_extraction_complete"])
    logging.info(LOG_MESSAGES["vep_ready"].format(path=f"{cache_root}/{species}"))
    Path(target_path).unlink()


def _chr_prefixed_standard_chrom(chrom: str) -> str | None:
    if chrom in {"M", "MT", "chrM", "chrMT"}:
        return "chrM"
    bare = chrom.removeprefix("chr")
    if bare.isdigit() or bare in {"X", "Y"}:
        return f"chr{bare}"
    return None


def _prefix_contig_header_line(line: str) -> str:
    prefix = "##contig=<ID="
    rest = line[len(prefix) :]
    for sep in (",", ">"):
        sep_idx = rest.find(sep)
        if sep_idx != -1:
            chrom = rest[:sep_idx]
            suffix = rest[sep_idx:]
            break
    else:
        return line

    prefixed = _chr_prefixed_standard_chrom(chrom)
    if prefixed:
        return f"{prefix}{prefixed}{suffix}"
    return line


def cmd_vep_verify(args: argparse.Namespace) -> None:
    vep_version = args.vep_version
    species = args.species
    assembly = args.assembly

    cache_root = _resolve_vep_cache_root(args)
    species_dir = str(Path(cache_root, species))
    version_dir = str(Path(species_dir, f"{vep_version}_{assembly}"))

    logging.info(
        LOG_MESSAGES["vep_verifying"].format(
            species=species, version=vep_version, assembly=assembly
        )
    )
    logging.info(LOG_MESSAGES["vep_location"].format(path=version_dir))

    if not Path(version_dir).exists():
        raise WGSExtractError(
            LOG_MESSAGES["vep_cache_missing"].format(path=version_dir)
        )

    # Check for basic files
    info_file = str(Path(version_dir, "info.txt"))
    if Path(info_file).exists():
        logging.info(LOG_MESSAGES["vep_info_found"])
    else:
        logging.warning(LOG_MESSAGES["vep_info_missing"])

    # Check for chromosomal directories
    missing_chrs = []
    for c in list(range(1, 23)) + ["X", "Y", "MT"]:
        chr_dir = str(Path(version_dir, str(c)))
        if not Path(chr_dir).exists():
            missing_chrs.append(str(c))

    if missing_chrs:
        logging.warning(
            LOG_MESSAGES["vep_chrs_missing"].format(chrs=", ".join(missing_chrs))
        )
    else:
        logging.info(LOG_MESSAGES["vep_chrs_ok"])

    # A cache without its info.txt or any chromosome isn't usable.
    if not Path(info_file).exists() or len(missing_chrs) == 25:
        raise WGSExtractError(
            f"{version_dir} is incomplete; download it again with "
            f"'wgsextract vep download --vep-version {vep_version} --assembly "
            f"{assembly}'."
        )
    logging.info(LOG_MESSAGES["vep_verification_complete"])


def preprocess_vcf_chr_prefix(input_path: str, output_path: str) -> None:
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
                line = _prefix_contig_header_line(line)
            elif line.startswith("#"):
                pass
            else:
                # Variant line
                parts = line.split("\t", 1)
                chrom = parts[0]
                new_chrom = _chr_prefixed_standard_chrom(chrom)
                if len(parts) > 1 and new_chrom:
                    line = f"{new_chrom}\t{parts[1]}"
            f_out.write(line)
