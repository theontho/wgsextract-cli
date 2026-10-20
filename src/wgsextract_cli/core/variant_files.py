import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence

from wgsextract_cli.core.messages import LOG_MESSAGES

from .alignment_metadata import (
    get_bam_header,
)
from .reference_resolver import (
    ReferenceLibrary,
    header_contig_lengths,
)
from .utils import (
    WGSExtractError,
    run_command,
)


def resolve_reference(
    ref_path: str | None, md5_sig: str | None, input_path: str | None = None
) -> str | None:
    """Find specific .fa.gz from directory or direct path."""
    lib = ReferenceLibrary(
        ref_path, md5_sig, skip_full_search=True, input_path=input_path
    )
    return lib.fasta if lib.fasta else ref_path


def verify_paths_exist(paths_dict: Mapping[str, str | None]) -> None:
    """Raise WGSExtractError unless every labelled path exists as a file."""
    problems: list[str] = []
    for label, path in paths_dict.items():
        if not path:
            problems.append(
                LOG_MESSAGES["file_not_found"].format(label=label, path=path)
            )
            continue

        normalized_path = os.path.expanduser(str(path))

        # Skip check for commands (like pixi run)
        if "pixi run" in normalized_path:
            continue

        if not os.path.exists(normalized_path) and not shutil.which(normalized_path):
            problems.append(
                LOG_MESSAGES["file_not_found"].format(label=label, path=normalized_path)
            )
        elif os.path.isdir(normalized_path) and not shutil.which(normalized_path):
            problems.append(
                f"{label} path is a directory, but a file is expected: {normalized_path}"
            )
    if problems:
        raise WGSExtractError("\n".join(problems))


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
    if os.path.exists(gz_path) and os.path.getmtime(gz_path) > os.path.getmtime(
        vcf_path
    ):
        ensure_vcf_indexed(gz_path)
        return gz_path

    from wgsextract_cli.core.dependencies import get_tool_path

    bgzip = get_tool_path("bgzip")
    if bgzip is None:
        raise WGSExtractError("bgzip dependency is required to compress VCF files.")
    logging.info(f"Compressing VCF: {vcf_path}")
    # Write to a temp name so an interrupted run never leaves a truncated .gz
    # that looks newer than the source and gets reused next time.
    tmp_path = gz_path + ".tmp"
    try:
        with open(tmp_path, "wb") as f_out:
            run_command([bgzip, "-c", vcf_path], stdout=f_out)
        os.replace(tmp_path, gz_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
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


def match_chromosome_name(target: str, available: Iterable[str]) -> str | None:
    """Return the alias of ``target`` (e.g. MT -> chrM) present in ``available``."""
    names = set(available)
    return next((alias for alias in chromosome_aliases(target) if alias in names), None)


def check_vcf_build(vcf_path: str, lengths: Mapping[str, int], against: str) -> None:
    """Fail when the VCF's ##contig lengths show a build other than ``against``'s."""
    contigs = list(lengths)
    mismatched = [
        f"{name} ({length:,} vs {lengths[other]:,})"
        for name, length in header_contig_lengths(get_bam_header(vcf_path)).items()
        if (other := match_chromosome_name(name, contigs)) and lengths[other] != length
    ]
    if mismatched:
        raise WGSExtractError(
            f"The VCF was called against a different reference than {against}; "
            f"contig lengths differ (VCF vs {against}): "
            + ", ".join(mismatched[:5])
            + (f" and {len(mismatched) - 5} more" if len(mismatched) > 5 else "")
        )


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
    vcf_path: str | None,
    target_chroms: Sequence[str],
    workdir: str | None = None,
) -> str:
    """
    Ensure VCF chromosome naming (chr1 vs 1) matches the targets.
    Returns path to a temporary normalized VCF (in ``workdir``, default the
    input's directory) if changes were needed.
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

    fd, map_file = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(f"{source} {target}" for source, target in mapping))

    # Use a unique temporary file for the normalized VCF to avoid collisions.
    # Free the name again so bcftools creates the file with the usual umask
    # permissions rather than mkstemp's 0600, which a final output would keep.
    fd_out, norm_vcf = tempfile.mkstemp(
        suffix=".vcf.gz", dir=workdir or os.path.dirname(vcf_path)
    )
    os.close(fd_out)
    os.remove(norm_vcf)

    logging.info(f"Normalizing chromosomes in {vcf_path}...")
    try:
        run_command(
            [
                "bcftools",
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
        return norm_vcf
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        if os.path.exists(norm_vcf):
            os.remove(norm_vcf)
        raise WGSExtractError(
            f"Chromosome normalization failed for {vcf_path}: {e}"
        ) from e
    finally:
        os.remove(map_file)


def get_chr_name(
    bam_path: str, target_chr: str, cram_opt: Sequence[str] | str | None = None
) -> str:
    """
    Dynamically map a target chromosome (like MT or Y) to the specific
    naming convention used in the BAM header (e.g., chrM, MT, M, chrY).
    """
    header = get_bam_header(bam_path, cram_opt)
    sn_names = [
        field[3:]
        for line in header.splitlines()
        if line.startswith("@SQ")
        for field in line.split("\t")
        if field.startswith("SN:")
    ]
    return match_chromosome_name(target_chr, sn_names) or target_chr


def get_vcf_chr_name(vcf_path: str, target_chr: str) -> str:
    """Map a target chromosome (like MT or Y) to the naming used in a VCF index."""
    return (
        match_chromosome_name(target_chr, vcf_index_chromosomes(vcf_path)) or target_chr
    )


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
