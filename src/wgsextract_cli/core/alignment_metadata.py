import logging
import subprocess
from collections.abc import Sequence
from os import PathLike, fspath

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import PathInput

from .builds import build_from_chr1_length
from .reference_assets import (
    header_sequences,
    read_fai_lengths,
    sequence_dictionary,
)
from .reference_models import sequence_lengths
from .utils import run_command


def get_bam_header(
    bam_path: PathInput, cram_opt: Sequence[str] | PathInput | None = None
) -> str:
    """Retrieve header using samtools view -H."""
    from wgsextract_cli.core.dependencies import get_tool_path

    bam_path = fspath(bam_path)
    is_cram = bam_path.lower().endswith(".cram")
    is_vcf = bam_path.lower().endswith((".vcf", ".vcf.gz", ".bgz", ".bcf"))

    if is_vcf:
        try:
            bcftools = get_tool_path("bcftools")
            if bcftools is None:
                return ""
            cmd = [bcftools, "view", "-h", bam_path]
            # Execute without check=True to avoid printing red error logs if it fails
            result = run_command(cmd, capture_output=True, check=False)
            if result.returncode == 0:
                return result.stdout
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            logging.debug("Unable to read VCF header with bcftools.", exc_info=True)
        return ""

    samtools = get_tool_path("samtools")
    if samtools is None:
        return ""
    cmd = [samtools, "view", "-H"]

    if is_cram and cram_opt:
        cmd.extend(
            ["-T", fspath(cram_opt)]
            if isinstance(cram_opt, (str, PathLike))
            else cram_opt
        )

    cmd.append(bam_path)

    try:
        result = run_command(cmd, capture_output=True)
        return result.stdout
    except (
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as e:
        # A CRAM header is readable without the reference; retry without -T.
        if is_cram and cram_opt:
            try:
                logging.debug(LOG_MESSAGES["util_header_retry"])
                result = run_command(
                    [samtools, "view", "-H", bam_path], capture_output=True
                )
                return result.stdout
            except (
                OSError,
                subprocess.SubprocessError,
                RuntimeError,
                ValueError,
                TypeError,
            ):
                logging.debug(
                    "Header fetch retry without CRAM reference failed.",
                    exc_info=True,
                )

        logging.error(LOG_MESSAGES["util_header_failed"].format(path=bam_path, error=e))
        return ""


def get_vcf_build(vcf_path: str) -> str | None:
    """Scan VCF header for build identifiers."""
    try:
        from wgsextract_cli.core.dependencies import get_tool_path

        bcftools = get_tool_path("bcftools")
        if bcftools is None:
            return None
        res = run_command([bcftools, "view", "-h", vcf_path], capture_output=True)
        # The ##contig lengths identify the assembly whatever the file names
        # it (chm13, human_g1k_v37, ...); the text search is the fallback.
        build = build_from_chr1_length(sequence_lengths(res.stdout))
        if build:
            return build
        header = res.stdout.lower()
        if "hg38" in header or "grch38" in header:
            return "hg38"
        if "hg19" in header or "grch37" in header:
            return "hg19"
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, TypeError):
        logging.debug("Unable to infer VCF build from header.", exc_info=True)
    return None


def cram_encoded_with(fasta: PathInput, cram: PathInput) -> bool:
    """Whether a CRAM's @SQ names, lengths and (where recorded) M5 sequence
    checksums all match a FASTA, so samtools can decode the CRAM with it.

    Names and lengths come from the .fai first: the M5s, which samtools dict
    computes by reading the whole FASTA when it has no .dict, are compared only
    when those match and the CRAM records them."""
    reads = header_sequences(get_bam_header(fspath(cram)))
    if not reads:
        return False
    names = [(r.get("SN"), r.get("LN")) for r in reads]
    lengths = read_fai_lengths(fasta)
    if lengths and names != [(n, str(ln)) for n, ln in lengths.items()]:
        return False
    if lengths and not any(r.get("M5") for r in reads):
        return True
    ref = sequence_dictionary(fasta)
    if not ref or names != [(f.get("SN"), f.get("LN")) for f in ref]:
        return False
    return all(
        not r.get("M5") or r["M5"].lower() == f.get("M5", "").lower()
        for r, f in zip(reads, ref, strict=True)
    )
