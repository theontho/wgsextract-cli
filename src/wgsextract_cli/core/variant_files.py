import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from typing import IO, Literal, TypeAlias, overload

from wgsextract_cli.core.messages import LOG_MESSAGES

try:
    import psutil
except ImportError:
    psutil = None


from .alignment_metadata import (
    get_bam_header,
)
from .reference_resolver import (
    ReferenceLibrary,
)
from .utils import (
    WGSExtractError,
    _normalize_subprocess_cmd,
    _process_group_kwargs,
    proc_registry,
    run_command,
)

ProcessStream: TypeAlias = int | IO[bytes] | IO[str] | None


def resolve_reference(
    ref_path: str | None, md5_sig: str | None, input_path: str | None = None
) -> str | None:
    """Find specific .fa.gz from directory or direct path."""
    lib = ReferenceLibrary(
        ref_path, md5_sig, skip_full_search=True, input_path=input_path
    )
    return lib.fasta if lib.fasta else ref_path


def calculate_bsd_sum(file_path: str) -> tuple[int, int]:
    """
    Calculates BSD-style checksum and 1K block count.
    Used for local file comparison.
    """
    checksum = 0
    blocks = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(1024):
            blocks += 1
            for byte in chunk:
                checksum = (checksum >> 1) + ((checksum & 1) << 15)
                checksum += byte
                checksum &= 0xFFFF
    return checksum, blocks


@overload
def popen(
    cmd: str | Sequence[str],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: Literal[False] = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]: ...


@overload
def popen(
    cmd: str | Sequence[str],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: Literal[True] = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[str]: ...


def popen(
    cmd: str | Sequence[str],
    stdout: ProcessStream = None,
    stderr: ProcessStream = None,
    stdin: ProcessStream = None,
    text: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
    """
    Helper to run subprocess.Popen with shlex splitting and registry.
    Default text=False to allow binary pipes (BAM/BCF).
    """
    cmd_list = _normalize_subprocess_cmd(cmd)

    cmd_str = " ".join(cmd_list)
    logging.debug(f"Popen: {cmd_str}")

    process = subprocess.Popen(
        cmd_list,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        text=text,
        env=env,
        **_process_group_kwargs(),
    )
    proc_registry.register_process(cmd_str, process)

    def _unregister_finished_process() -> None:
        process.wait()
        proc_registry.unregister_process(cmd_str, process)

    threading.Thread(target=_unregister_finished_process, daemon=True).start()
    return process


def verify_paths_exist(paths_dict: Mapping[str, str | None]) -> bool:
    """Validate that required files exist before starting a command."""
    all_exist = True
    for label, path in paths_dict.items():
        if not path:
            logging.error(LOG_MESSAGES["file_not_found"].format(label=label, path=path))
            all_exist = False
            continue

        normalized_path = os.path.expanduser(str(path))

        # Skip check for commands (like pixi run)
        if "pixi run" in normalized_path:
            continue

        if not os.path.exists(normalized_path) and not shutil.which(normalized_path):
            logging.error(
                LOG_MESSAGES["file_not_found"].format(label=label, path=normalized_path)
            )
            all_exist = False
        elif os.path.isdir(normalized_path) and not shutil.which(normalized_path):
            logging.error(
                f"{label} path is a directory, but a file is expected: {normalized_path}"
            )
            all_exist = False
    return all_exist


def ensure_vcf_indexed(vcf_path: str) -> None:
    """Ensure VCF has a .tbi index, creating it if needed."""
    if not (vcf_path.endswith(".gz") or vcf_path.endswith(".bgz")):
        # We can't index plain VCF easily with tabix
        return

    index_path = vcf_path + ".tbi"
    csi_index_path = vcf_path + ".csi"
    if not os.path.exists(index_path) and not os.path.exists(csi_index_path):
        from wgsextract_cli.core.dependencies import get_tool_path

        tabix = get_tool_path("tabix")
        if tabix is None:
            raise WGSExtractError("tabix dependency is required to index VCF files.")
        logging.info(LOG_MESSAGES["vcf_indexing"].format(path=vcf_path))
        run_command([tabix, "-p", "vcf", vcf_path])


def ensure_vcf_prepared(vcf_path: str | None) -> str:
    """Ensure VCF is bgzipped and indexed, returns path to compressed file."""
    if not vcf_path:
        raise WGSExtractError("VCF input path is required.")

    # If it's a BAM or CRAM, don't try to prepare it as a VCF
    if vcf_path.lower().endswith((".bam", ".cram")):
        return vcf_path

    if vcf_path.endswith(".gz") or vcf_path.endswith(".bgz"):
        ensure_vcf_indexed(vcf_path)
        return vcf_path

    # Need to bgzip
    gz_path = vcf_path + ".gz"
    if os.path.exists(gz_path):
        # Check if gz is newer than raw
        if os.path.getmtime(gz_path) > os.path.getmtime(vcf_path):
            ensure_vcf_indexed(gz_path)
            return gz_path

    from wgsextract_cli.core.dependencies import get_tool_path

    bgzip = get_tool_path("bgzip")
    logging.info(f"Compressing VCF: {vcf_path}")
    with open(gz_path, "wb") as f_out:
        run_command([bgzip, "-c", vcf_path], stdout=f_out)
    ensure_vcf_indexed(gz_path)
    return gz_path


def get_vcf_samples(vcf_path: str) -> list[str]:
    """Retrieve sample names from VCF header."""
    try:
        res = run_command(["bcftools", "query", "-l", vcf_path], capture_output=True)
        stdout = str(res.stdout)
        return stdout.strip().split("\n") if stdout.strip() else []
    except (OSError, subprocess.SubprocessError, WGSExtractError):
        return []


def chromosome_aliases(chrom: str) -> tuple[str, ...]:
    if not chrom:
        return ()

    if chrom.startswith("chr"):
        bare = chrom[3:]
    else:
        bare = chrom

    aliases: tuple[str, ...]
    if bare.upper() in {"M", "MT"}:
        aliases = ("chrM", "chrMT", "MT", "M")
    elif chrom.startswith("chr"):
        aliases = (chrom, bare)
    else:
        aliases = (chrom, f"chr{chrom}")

    return tuple(dict.fromkeys(aliases))


def chromosome_rename_mapping(
    source_chroms: Sequence[str], target_chroms: Sequence[str]
) -> list[tuple[str, str]]:
    """Build bcftools --rename-chrs mappings from source names to target names."""
    targets = {chrom for chrom in target_chroms if chrom}
    target_by_alias: dict[str, str] = {}
    for target in target_chroms:
        if not target:
            continue
        for alias in chromosome_aliases(target):
            target_by_alias.setdefault(alias, target)

    mapping: list[tuple[str, str]] = []
    for source in source_chroms:
        if not source or source in targets:
            continue
        matched_target = target_by_alias.get(source)
        if matched_target and matched_target != source:
            mapping.append((source, matched_target))
    return mapping


def vcf_index_chromosomes(vcf_path: str) -> list[str]:
    """Return contigs listed in a bgzipped VCF index."""
    try:
        res = run_command(["bcftools", "index", "-s", vcf_path], capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Could not inspect VCF chromosome index for {vcf_path}: {e}")
        return []
    return [
        line.split("\t", 1)[0]
        for line in (res.stdout or "").splitlines()
        if line.strip()
    ]


def normalize_vcf_chromosomes(
    vcf_path: str | None, target_chroms: Sequence[str]
) -> str:
    """
    Ensure VCF chromosome naming (chr1 vs 1) matches the targets.
    Returns path to a temporary normalized VCF if changes were needed.
    """
    if not vcf_path:
        raise WGSExtractError(
            "VCF input path is required for chromosome normalization."
        )

    v_chroms = vcf_index_chromosomes(vcf_path)
    if not v_chroms:
        logging.warning(f"Chromosome normalization skipped for {vcf_path}.")
        return vcf_path

    mapping = chromosome_rename_mapping(v_chroms, target_chroms)
    if not mapping:
        return vcf_path

    # Create mapping file
    fd, map_file = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(f"{source} {target}" for source, target in mapping))

    # Use a unique temporary file for the normalized VCF to avoid collisions
    fd_out, norm_vcf = tempfile.mkstemp(suffix=".vcf.gz", dir=os.path.dirname(vcf_path))
    os.close(fd_out)

    logging.info(f"Normalizing chromosomes in {vcf_path}...")
    try:
        from wgsextract_cli.core.dependencies import get_tool_path

        bcftools = get_tool_path("bcftools")
        run_command(
            [
                bcftools,
                "annotate",
                "--rename-chrs",
                map_file,
                "-Oz",
                "-o",
                norm_vcf,
                vcf_path,
            ]
        )
        ensure_vcf_indexed(norm_vcf)
        os.remove(map_file)
        return norm_vcf
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"Normalization failed: {e}")
        if os.path.exists(map_file):
            os.remove(map_file)
        if os.path.exists(norm_vcf):
            os.remove(norm_vcf)
        return vcf_path


def get_chr_name(
    bam_path: str, target_chr: str, cram_opt: Sequence[str] | str | None = None
) -> str:
    """
    Dynamically map a target chromosome (like MT or Y) to the specific
    naming convention used in the BAM header (e.g., chrM, MT, M, chrY).
    """
    header = get_bam_header(bam_path, cram_opt)
    sq_lines = [line for line in header.splitlines() if line.startswith("@SQ")]

    # Extract SN fields
    sn_names = []
    for line in sq_lines:
        parts = line.split("\t")
        for part in parts:
            if part.startswith("SN:"):
                sn_names.append(part[3:])
                break

    if target_chr.upper() in ["M", "MT", "CHRM", "CHRMT"]:
        for c in ["chrM", "chrMT", "MT", "M"]:
            if c in sn_names:
                return c
    elif target_chr.upper() in ["Y", "CHRY"]:
        for c in ["chrY", "Y"]:
            if c in sn_names:
                return c

    return target_chr  # fallback


def calculate_bam_md5(
    bam_path: str,
    cram_opt: Sequence[str] | str | None = None,
    header: str | None = None,
) -> str:
    """
    Calculates MD5 signature from BAM header @SQ lines.
    It takes SN and LN, upcases SN, sorts ASCII-wise, and hashes.
    If a @CO line with MD5: exists, it returns that instead.
    """
    if header is None:
        header = get_bam_header(bam_path, cram_opt)
    if not header:
        return "00000000000000000000000000000000"

    # Check for embedded MD5 signature in comments or read groups first
    for line in header.splitlines():
        if (line.startswith("@CO") or line.startswith("@RG")) and "MD5:" in line:
            parts = line.split("MD5:")
            if len(parts) > 1:
                sig = parts[1].strip().split()[0]
                if len(sig) == 32:
                    return sig

    sq_lines = [line for line in header.splitlines() if line.startswith("@SQ")]

    bamsq_header: dict[str, str] = {}
    for line in sq_lines:
        sn = None
        ln = None
        for part in line.split("\t"):
            if part.startswith("SN:"):
                sn = part[3:].upper()
            elif part.startswith("LN:"):
                ln = part[3:]
        if sn and ln:
            bamsq_header[sn] = ln

    pseudo_header = ""
    for key in sorted(bamsq_header.keys()):
        pseudo_header += f"SN:{key}\tLN:{bamsq_header[key]}\n"

    md5hash = hashlib.md5(pseudo_header.encode())
    return md5hash.hexdigest()


def is_sorted(
    bam_path: str,
    cram_opt: Sequence[str] | str | None = None,
    header: str | None = None,
) -> bool:
    """Verifies if BAM is coordinate sorted."""
    if header is None:
        header = get_bam_header(bam_path, cram_opt)
    if not header:
        return False
    for line in header.splitlines():
        if line.startswith("@HD"):
            if "SO:coordinate" in line:
                return True
    return False


def get_ref_mito(
    bam_path: str,
    cram_opt: Sequence[str] | str | None = None,
    header: str | None = None,
) -> str:
    """
    Identifies if mitochondrial reference is Yoruba based on chromosome lengths.
    Logic from program/bamfiles.py.
    """
    if header is None:
        header = get_bam_header(bam_path, cram_opt)
    if not header:
        return "rCRS"

    # Check for Build 37 with Yoruba length (16571)
    is_build37 = any(
        x in header for x in ["LN:249250621", "LN:59373566", "LN:155270560"]
    )
    if is_build37 and any(x in header for x in ["M\tLN:16571", "MT\tLN:16571"]):
        return "Yoruba"

    # Check for other Yoruba builds (15, 16, 17, 18)
    if any(x in header for x in ["LN:247249719", "LN:57772954", "LN:154913754"]):
        return "Yoruba"  # Build 18
    if any(x in header for x in ["LN:245522847", "LN:57701691", "LN:154824264"]):
        return "Yoruba"  # Build 17
    if any(x in header for x in ["LN:246127941", "LN:50286555", "LN:153692391"]):
        return "Yoruba"  # Build 16
    if any(x in header for x in ["LN:245203898", "LN:50961097", "LN:152634166"]):
        return "Yoruba"  # Build 15

    return "rCRS"  # Default


def get_file_version(filepath: str) -> str:
    """Retrieve the format and version of a genomic file using htsfile."""
    try:
        res = run_command(["htsfile", filepath], capture_output=True, check=False)
        if res.returncode == 0:
            # Output format: "path: Format version X sequence data"
            # e.g., "file.cram: CRAM version 3.1 sequence data"
            out = str(res.stdout).strip()
            if ":" in out:
                version_info = out.split(":", 1)[1].strip()
                # Clean up "sequence data" suffix if present
                version_info = version_info.replace(" sequence data", "")
                return version_info
    except (OSError, subprocess.SubprocessError):
        logging.debug("Could not determine file version with htsfile for %s", filepath)
    return "Unknown"
