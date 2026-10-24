from __future__ import annotations

import filecmp
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from wgsextract_cli.core.bgzf import ensure_bgzf
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.reference_assets import (
    fasta_prefix,
    n_count_paths,
    reference_dict_path,
)
from wgsextract_cli.core.utils import (
    WGSExtractError,
    popen,
    run_command,
    wait_with_cancel,
)

from .annotation_resources import (
    GNOMAD_URLS,
    CancelEvent,
    ProgressCallback,
)
from .ref_library.catalog import get_genome_status
from .ref_library.downloads import download_file

_PLOIDY_TEMPLATES = {
    "GRCh37": """X 1 60000 M 1
X 2699521 154931043 M 1
Y 1 59373566 M 1
Y 1 59373566 F 0
MT 1 16569 M 1
MT 1 16569 F 1
* * * M 2
* * * F 2
""",
    "GRCh38": """X 1 10000 M 1
X 2781480 155701383 M 1
Y 1 57227415 M 1
Y 1 57227415 F 0
MT 1 16569 M 1
MT 1 16569 F 1
* * * M 2
* * * F 2
""",
}


def download_gnomad(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and indexes gnomAD sites VCFs for hg19 and hg38."""
    target_dir = str(Path(reflib_dir, "ref"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    success = True
    for build, url in GNOMAD_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        dest_path = str(Path(target_dir, f"gnomad_{build}.vcf.bgz"))
        logging.info(f"Downloading gnomAD {build} from Google Storage...")

        if not download_file(url, dest_path, progress_callback, cancel_event):
            success = False
            continue

        if cancel_event and cancel_event.is_set():
            return False

        # Index the VCF
        logging.info(f"Indexing gnomAD {build}...")
        try:
            # gnomAD VCFs are usually already bgzipped and indexed,
            # but we might need to download the index or recreate it.
            # Tabix -p vcf works for .vcf.bgz as well.
            run_command(["tabix", "-p", "vcf", "-f", dest_path])
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Failed to index gnomAD {build}: {e}")
            success = False

    return success


def delete_genome(final_name: str, reflib_dir: str) -> bool:
    base_path = str(Path(reflib_dir, "genomes", final_name))
    paths = [base_path + ext for ext in ["", ".partial", ".fai", ".gzi", ".dict"]]
    paths.extend([reference_dict_path(base_path), *n_count_paths(base_path)])
    paths.append(fasta_prefix(base_path) + ".wgse")
    for path in paths:
        if Path(path).exists():
            Path(path).unlink()
    return True


def has_ref_ns(final_name: str, reflib_dir: str) -> bool:
    """Checks if N-count files exist for a reference genome."""
    if not reflib_dir:
        return False
    base_path = str(Path(reflib_dir, "genomes", final_name))
    return any(Path(path).exists() for path in n_count_paths(base_path))


def _run_reference_tool(cmd: list[str], cancel_event: CancelEvent | None) -> bool:
    """Run an indexing tool; False if cancelled, WGSExtractError with stderr on failure."""
    with tempfile.TemporaryFile() as stderr_file:
        process = popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_file)
        if wait_with_cancel(process, cancel_event):
            return True
        if cancel_event and cancel_event.is_set():
            return False
        stderr_file.seek(0)
        stderr = stderr_file.read().decode(errors="replace").strip()
    raise WGSExtractError(
        f"{' '.join(cmd[:2])} failed with exit code {process.returncode}: "
        f"{stderr[-2000:] or 'no error output'}"
    )


def process_reference_file(
    fasta_path: str,
    status_callback: Callable[[str], None] | None = None,
    cancel_event: CancelEvent | None = None,
) -> bool:
    logging.info(f"Processing reference: {fasta_path}")

    bgzf_path = ensure_bgzf(fasta_path, status_callback, cancel_event)
    if not bgzf_path:
        return False

    if cancel_event and cancel_event.is_set():
        return False

    dict_path = reference_dict_path(bgzf_path)
    try:
        logging.info("Generating sequence dictionary...")
        if status_callback:
            status_callback("Processing: Generating Dictionary...")
        if not _run_reference_tool(
            ["samtools", "dict", bgzf_path, "-o", dict_path], cancel_event
        ):
            return False

        logging.info("Indexing FASTA...")
        if status_callback:
            status_callback("Processing: Indexing FASTA...")
        if not _run_reference_tool(["samtools", "faidx", bgzf_path], cancel_event):
            return False

    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        if sys.platform == "win32" and isinstance(e, FileNotFoundError):
            logging.warning(
                f"Warning: Could not index reference on Windows (missing tools): {e}"
            )
            logging.warning("Reference is downloaded but remains unindexed.")
            return True
        logging.error(f"Indexing failed: {e}")
        return False

    return True


def download_and_process_genome(
    genome_data: Mapping[str, str],
    reflib_dir: str,
    progress_callback: ProgressCallback | None = None,
    cancel_event: CancelEvent | None = None,
    restart: bool = False,
    status_callback: Callable[[str], None] | None = None,
) -> bool:
    """Download (resuming a .partial) and index a genome; restart discards existing data.

    An already-installed genome is only re-indexed unless restart is set.
    """
    verify_dependencies(["samtools", "bgzip", "gzip"])
    target_dir = str(Path(reflib_dir, "genomes"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    final_path = str(Path(target_dir, genome_data["final"]))
    partial_path = final_path + ".partial"

    if restart:
        if Path(final_path).exists():
            Path(final_path).unlink()
        if Path(partial_path).exists():
            Path(partial_path).unlink()

    if get_genome_status(genome_data["final"], reflib_dir) == "installed":
        return process_reference_file(final_path, status_callback, cancel_event)

    logging.info(f"Downloading {genome_data['label']} from {genome_data['source']}...")

    success = download_file(
        genome_data["url"], final_path, progress_callback, cancel_event
    )
    if not success:
        return False

    return process_reference_file(final_path, status_callback, cancel_event)


def download_bootstrap(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Downloads and extracts the reference library bootstrap."""
    from wgsextract_cli.core.constants import BOOTSTRAP_FILENAME, BOOTSTRAP_URL

    if not Path(reflib_dir).exists():
        Path(reflib_dir).mkdir(parents=True, exist_ok=True)

    normalize_bootstrap_layout(reflib_dir)
    if bootstrap_has_support_assets(reflib_dir):
        logging.info("Reference bootstrap assets are already installed.")
        return install_bootstrap_support_files(reflib_dir)

    dest_path = str(Path(reflib_dir, BOOTSTRAP_FILENAME))
    logging.info(f"Downloading bootstrap from {BOOTSTRAP_URL}...")

    for attempt in range(1, 4):
        if download_file(BOOTSTRAP_URL, dest_path, progress_callback, cancel_event):
            break
        if attempt == 3:
            return False
        logging.warning("Bootstrap download failed; retrying (%s/3)...", attempt + 1)

    logging.info(f"Extracting bootstrap to {reflib_dir}...")
    try:
        extract_archive_keep_existing(dest_path, reflib_dir)
        Path(dest_path).unlink()
        normalize_bootstrap_layout(reflib_dir)
        if not install_bootstrap_support_files(reflib_dir):
            return False
        logging.info("Bootstrap extraction complete.")
        return True
    except (OSError, tarfile.TarError, WGSExtractError) as e:
        logging.error(f"Failed to extract bootstrap: {e}")
        return False


def extract_archive_keep_existing(archive_path: str, dest_dir: str) -> None:
    """Extract regular files and directories from a tar archive, never overwriting.

    Equivalent to `tar -xkf` on every platform. Members that would land outside
    dest_dir are rejected; links and special files are skipped, as is macOS
    AppleDouble/Finder metadata.
    """
    dest_root = Path(dest_dir).resolve()
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                name.name.startswith("._")
                or name.name == ".DS_Store"
                or "__MACOSX" in name.parts
            ):
                continue
            target = (dest_root / name).resolve()
            if (
                name.is_absolute()
                or ".." in name.parts
                or not target.is_relative_to(dest_root)
            ):
                raise WGSExtractError(f"Unsafe path in archive: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                logging.warning("Skipping non-regular archive member: %s", member.name)
                continue
            if target.exists():
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")
            with source, open(partial, "wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            Path(partial).replace(target)


def bootstrap_has_support_assets(reflib_dir: str) -> bool:
    if not Path(reflib_dir).is_dir():
        return False
    names = {
        "All_SNPs.vcf.gz",
        "common_all.vcf.gz",
        "snps_hg19.vcf.gz",
        "snps_hg38.vcf.gz",
        "snps_grch37.vcf.gz",
        "snps_grch38.vcf.gz",
    }
    for _, _, files in os.walk(reflib_dir):
        for file_name in files:
            if file_name in names:
                return True
            if file_name.endswith("_ref.tab.gz"):
                return True
            if file_name.startswith("All_SNPs") and file_name.endswith(".tab.gz"):
                return True
    return False


def normalize_bootstrap_layout(reflib_dir: str) -> None:
    nested = str(Path(reflib_dir, "reference"))
    if not Path(nested).is_dir():
        _remove_macos_metadata(reflib_dir)
        return
    _merge_directory(nested, reflib_dir)
    _remove_empty_directories(nested)
    if Path(nested).is_dir() and not os.listdir(nested):
        Path(nested).rmdir()
    _remove_macos_metadata(reflib_dir)


def install_bootstrap_support_files(reflib_dir: str) -> bool:
    ok = install_ploidy_files(reflib_dir)
    if ok:
        logging.info("Reference bootstrap support files are ready.")
    return ok


def install_ploidy_files(reflib_dir: str) -> bool:
    outputs = {
        "GRCh37": str(Path(reflib_dir, "ploidy_hg19.txt")),
        "GRCh38": str(Path(reflib_dir, "ploidy_hg38.txt")),
    }
    ok = True
    for alias, output in outputs.items():
        if Path(output).is_file():
            logging.info("Ploidy file already installed: %s", output)
            continue
        logging.info("Installing ploidy file for %s: %s", alias, output)
        try:
            content = _ploidy_content(alias)
            with open(output, "w", encoding="utf-8") as ploidy_file:
                ploidy_file.write(content)
        except OSError as e:
            logging.error("Failed to install ploidy file %s: %s", output, e)
            ok = False
    return ok


def _ploidy_content(alias: str) -> str:
    try:
        result = run_command(
            ["bcftools", "call", "--ploidy", f"{alias}?"],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        stdout = getattr(result, "stdout", "")
        if stdout and "*" in stdout:
            return str(stdout)
    except (OSError, subprocess.SubprocessError):
        logging.debug("Could not query bcftools built-in ploidy for %s.", alias)
    return _PLOIDY_TEMPLATES[alias]


def _merge_directory(src: str, dst: str) -> None:
    Path(dst).mkdir(parents=True, exist_ok=True)
    for name in os.listdir(src):
        source = str(Path(src, name))
        if name.startswith("._") or name == ".DS_Store":
            _remove_path(source)
            continue
        target = str(Path(dst, name))
        if Path(source).is_dir() and not Path(source).is_symlink():
            _merge_directory(source, target)
            if not os.listdir(source):
                Path(source).rmdir()
        elif not Path(target).exists():
            shutil.move(source, target)
        elif (
            Path(source).is_file()
            and Path(target).is_file()
            and filecmp.cmp(source, target, shallow=False)
        ):
            Path(source).unlink()
        else:
            logging.warning("Leaving duplicate bootstrap file in place: %s", source)


def _remove_macos_metadata(root: str) -> None:
    if not Path(root).is_dir():
        return
    for current_dir, dirs, files in os.walk(root):
        for file_name in files:
            if file_name.startswith("._") or file_name == ".DS_Store":
                _remove_path(str(Path(current_dir, file_name)))
        for directory in list(dirs):
            if directory == "__MACOSX":
                _remove_path(str(Path(current_dir, directory)))
                dirs.remove(directory)


def _remove_empty_directories(root: str) -> None:
    if not Path(root).is_dir():
        return
    for current_dir, _, _ in os.walk(root, topdown=False):
        if current_dir != root and not os.listdir(current_dir):
            Path(current_dir).rmdir()


def _remove_path(path: str) -> None:
    if Path(path).is_dir() and not Path(path).is_symlink():
        shutil.rmtree(path)
    elif Path(path).exists():
        Path(path).unlink()
