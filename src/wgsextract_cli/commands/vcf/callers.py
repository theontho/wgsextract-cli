import argparse
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.reference_assets import reference_dict_path
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

from .basic import get_base_args, plain_reference


def _prepare_gatk_input(
    args: argparse.Namespace, ref: str, outdir: str
) -> tuple[str, str | None]:
    """Return an input GATK can read, converting regional CRAM input when needed."""
    if not args.input.lower().endswith(".cram"):
        return args.input, None

    if not args.region:
        logging.warning(
            "GATK may not support CRAM 3.1 directly. For whole-genome CRAM input, "
            "convert to BAM first with 'wgsextract bam to-bam' or pass --region for "
            "automatic regional conversion."
        )
        return args.input, None

    import tempfile

    fd, temp_bam = tempfile.mkstemp(suffix=".bam", dir=outdir)
    os.close(fd)
    logging.info(f"Preparing regional BAM for GATK from CRAM input ({args.region})...")
    try:
        samtools = get_tool_path("samtools")
        run_command(
            [
                samtools,
                "view",
                "-bh",
                "-T",
                ref,
                "-o",
                temp_bam,
                args.input,
                args.region,
            ],
            check=True,
        )
        run_command([samtools, "index", temp_bam], check=True)
    except (OSError, subprocess.SubprocessError, WGSExtractError):
        _cleanup_gatk_temp_input(temp_bam)
        raise
    return temp_bam, temp_bam


def _gatk_region_args(region: str | None, outdir: str) -> tuple[list[str], str | None]:
    """-L through an interval-list file: on Windows GATK parses a bare `20:1-200`
    as a file path and fails on the ':'; a .intervals file reads the same everywhere."""
    if not region:
        return [], None
    intervals = str(Path(outdir, "gatk_region.intervals"))
    Path(intervals).write_text(region + "\n", encoding="utf-8")
    return ["-L", intervals], intervals


def _gatk_tmp_dir() -> str:
    """A temp dir for GATK without spaces. GATK 4.6 can't load its codecs'
    bundled config from a temp path with a space ("Unable to automatically
    instantiate codec"), and on Windows %TEMP% is under the user's profile, so
    any user whose name has a space would hit it; use the 8.3 short path there."""
    path = tempfile.mkdtemp(prefix="wgse-gatk-")
    if " " in path and sys.platform == "win32":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
            path = buffer.value
    if " " in path:
        logging.warning(
            f"GATK's temp directory has a space ({path}); GATK may fail to start."
        )
    return path


def _cleanup_gatk_temp_input(temp_input: str | None) -> None:
    if not temp_input:
        return
    for path in (temp_input, temp_input + ".bai", temp_input + ".csi"):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logging.warning(f"Failed to remove temporary GATK artifact {path}: {e}")


def cmd_freebayes(args: argparse.Namespace) -> None:
    verify_dependencies(["freebayes", "bcftools", "tabix", "samtools"])
    _threads, outdir, ref, _lib = get_base_args(args)

    out_vcf = str(Path(outdir, "freebayes.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_freebayes"].format(output=out_vcf))

    # FreeBayes needs an uncompressed reference.
    with plain_reference(ref, outdir) as use_ref:
        if args.input.lower().endswith(".cram"):
            # Decode CRAM with samtools and stream uncompressed BAM to freebayes.
            view_cmd = ["samtools", "view", "-u", "-h", "-T", use_ref, args.input]
            if args.region:
                view_cmd.append(args.region)
            stages = [view_cmd, ["freebayes", "-f", use_ref, "--stdin"]]
        else:
            region_args = ["-r", args.region] if args.region else []
            stages = [["freebayes", "-f", use_ref, *region_args, args.input]]
        stages.append(["bcftools", "view", "-Oz", "-o", out_vcf])
        run_pipeline(stages, description="FreeBayes variant calling")
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)


def cmd_gatk(args: argparse.Namespace) -> None:
    verify_dependencies(["gatk", "samtools"])
    _threads, outdir, ref, lib = get_base_args(args)

    out_vcf = str(Path(outdir, "gatk.vcf.gz"))

    # GATK requires a .dict file
    if not lib.dict_file:
        logging.info(LOG_MESSAGES["vcf_generating_dict"])
        dict_file = reference_dict_path(ref)
        try:
            run_command(["samtools", "dict", "-o", dict_file, ref], check=True)
            lib.dict_file = dict_file
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Failed to generate GATK dict file: {e}") from e

    logging.info(LOG_MESSAGES["vcf_calling_gatk"].format(output=out_vcf))
    region_args, region_file = _gatk_region_args(args.region, outdir)
    gatk_input = args.input
    temp_gatk_input = None
    gatk_tmp = _gatk_tmp_dir()

    try:
        gatk_input, temp_gatk_input = _prepare_gatk_input(args, ref, outdir)
        cmd = [
            "gatk",
            "HaplotypeCaller",
            "-R",
            ref,
            "-I",
            gatk_input,
            "-O",
            out_vcf,
            "--tmp-dir",
            gatk_tmp,
        ] + region_args
        run_command(cmd)
        ensure_vcf_indexed(out_vcf)
        warn_if_no_variants(out_vcf, ref, args.region)
    except subprocess.CalledProcessError as e:
        logging.error(f"GATK failed: {e}")
        if args.input.lower().endswith(".cram"):
            logging.error(
                "Hint: Older GATK versions do not support CRAM version 3.1. "
                "If this failed with a CRAM error, please convert to BAM or upgrade GATK."
            )
        raise WGSExtractError("GATK failed.") from e
    finally:
        _cleanup_gatk_temp_input(temp_gatk_input)
        if region_file:
            Path(region_file).unlink(missing_ok=True)
        shutil.rmtree(gatk_tmp, ignore_errors=True)
