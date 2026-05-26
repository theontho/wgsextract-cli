import logging
import os
import subprocess
from collections.abc import Sequence

from wgsextract_cli.core.messages import LOG_MESSAGES

try:
    import psutil
except ImportError:
    psutil = None


from .utils import (
    run_command,
)


def get_bam_header(bam_path: str, cram_opt: Sequence[str] | str | None = None) -> str:
    """Retrieve header using samtools view -H."""
    from wgsextract_cli.core.dependencies import get_tool_path

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
        if not isinstance(cram_opt, str):
            cmd.extend(cram_opt)
        elif os.path.isfile(cram_opt):
            cmd.extend(["-T", cram_opt])
        else:
            # Maybe it's a directory, samtools -T needs a file.
            cmd.extend(["-T", cram_opt])

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
        # Attempt 2: Try bcftools view -h (often more robust for CRAM)
        try:
            logging.debug("Header fetch with samtools failed, trying bcftools...")
            bcftools = get_tool_path("bcftools")
            if bcftools is None:
                raise RuntimeError("bcftools is not available for header retry.")
            bcftools_cmd = [bcftools, "view", "-h", bam_path]
            result = run_command(bcftools_cmd, capture_output=True)
            return result.stdout
        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            ValueError,
            TypeError,
        ):
            logging.debug("Header fetch retry with bcftools failed.", exc_info=True)

        # Attempt 3: If it's a CRAM and we had a reference, try WITHOUT it.
        if is_cram:
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
        header = res.stdout.lower()
        if "hg38" in header or "grch38" in header:
            return "hg38"
        if "hg19" in header or "grch37" in header:
            return "hg19"
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, TypeError):
        logging.debug("Unable to infer VCF build from header.", exc_info=True)
    return None
