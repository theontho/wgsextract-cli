import hashlib
import logging
import os
import shutil
import subprocess
import tempfile

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
    _normalize_subprocess_cmd,
    _process_group_kwargs,
    proc_registry,
    run_command,
)


def resolve_reference(ref_path, md5_sig, input_path=None):
    """Find specific .fa.gz from directory or direct path."""
    lib = ReferenceLibrary(
        ref_path, md5_sig, skip_full_search=True, input_path=input_path
    )
    return lib.fasta if lib.fasta else ref_path


def calculate_bsd_sum(file_path):
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


def popen(cmd, stdout=None, stderr=None, stdin=None, text=False, env=None):
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
    return process


def verify_paths_exist(paths_dict):
    """Validate that required files exist before starting a command."""
    all_exist = True
    for label, path in paths_dict.items():
        if not path:
            logging.error(LOG_MESSAGES["file_not_found"].format(label=label, path=path))
            all_exist = False
            continue

        # Skip check for commands (like pixi run)
        if "pixi run" in str(path):
            continue

        if not os.path.exists(str(path)) and not shutil.which(str(path)):
            logging.error(LOG_MESSAGES["file_not_found"].format(label=label, path=path))
            all_exist = False
        elif os.path.isdir(str(path)) and not shutil.which(str(path)):
            logging.error(
                f"{label} path is a directory, but a file is expected: {path}"
            )
            all_exist = False
    return all_exist


def ensure_vcf_indexed(vcf_path):
    """Ensure VCF has a .tbi index, creating it if needed."""
    if not (vcf_path.endswith(".gz") or vcf_path.endswith(".bgz")):
        # We can't index plain VCF easily with tabix
        return

    index_path = vcf_path + ".tbi"
    if not os.path.exists(index_path):
        from wgsextract_cli.core.dependencies import get_tool_path

        tabix = get_tool_path("tabix")
        logging.info(LOG_MESSAGES["vcf_indexing"].format(path=vcf_path))
        run_command([tabix, "-p", "vcf", vcf_path])


def ensure_vcf_prepared(vcf_path):
    """Ensure VCF is bgzipped and indexed, returns path to compressed file."""
    if not vcf_path:
        return vcf_path

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


def get_vcf_samples(vcf_path):
    """Retrieve sample names from VCF header."""
    try:
        res = run_command(["bcftools", "query", "-l", vcf_path], capture_output=True)
        return res.stdout.strip().split("\n")
    except Exception:
        return []


def normalize_vcf_chromosomes(vcf_path, target_chroms):
    """
    Ensure VCF chromosome naming (chr1 vs 1) matches the targets.
    Returns path to a temporary normalized VCF if changes were needed.
    """
    v_chroms = []
    try:
        res = run_command(["bcftools", "index", "-s", vcf_path], capture_output=True)
        v_chroms = [line.split("\t")[0] for line in res.stdout.strip().split("\n")]
    except Exception:
        return vcf_path

    needs_rename = False
    mapping = []

    # Check for mismatches
    for tc in target_chroms:
        # Simple match
        if tc in v_chroms:
            continue

        # Try prefix mismatch
        if tc.startswith("chr"):
            alt = tc[3:]
        else:
            alt = "chr" + tc

        if alt in v_chroms:
            needs_rename = True
            mapping.append(f"{alt} {tc}")

    if not needs_rename:
        return vcf_path

    # Create mapping file
    fd, map_file = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(mapping))

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
    except Exception as e:
        logging.error(f"Normalization failed: {e}")
        if os.path.exists(map_file):
            os.remove(map_file)
        if os.path.exists(norm_vcf):
            os.remove(norm_vcf)
        return vcf_path


def get_chr_name(bam_path, target_chr, cram_opt=None):
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


def calculate_bam_md5(bam_path, cram_opt=None, header=None):
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

    bamsq_header = {}
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


def is_sorted(bam_path, cram_opt=None, header=None):
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


def get_ref_mito(bam_path, cram_opt=None, header=None):
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
    except Exception:
        pass
    return "Unknown"
