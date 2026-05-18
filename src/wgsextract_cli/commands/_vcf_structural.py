import gzip
import logging
import os
import shutil
import subprocess
import sys

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    verify_paths_exist,
)

from ._vcf_basic import (
    get_base_args,
)


def cmd_cnv(args):
    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base

    out_bcf = os.path.join(outdir, "cnv.bcf")
    out_vcf = os.path.join(outdir, "cnv.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_cnv"].format(output=out_vcf))

    # Auto-resolve mappability map if possible
    map_file = (
        args.map
        if getattr(args, "map", None)
        else getattr(lib, "mappability_map", None)
    )

    if not map_file or not os.path.exists(map_file):
        msg = "Mappability map (-M/--map) is required for delly cnv.\nYou can download standard maps (e.g., hg38.map.gz) from:\nhttps://github.com/dellytools/delly/tree/master/exclude\nOr provide a custom .map file."
        logging.error(f"❌: {msg}")
        raise WGSExtractError(msg) from None

    map_args = ["-m", map_file]
    if getattr(args, "ploidy", None):
        map_args.extend(["-y", args.ploidy])

    temp_bam = None
    input_file = args.input

    if getattr(args, "region", None):
        import tempfile

        fd, temp_bam = tempfile.mkstemp(suffix=".bam", dir=outdir)
        os.close(fd)
        logging.info(f"Extracting region {args.region} to temporary BAM...")
        try:
            samtools = get_tool_path("samtools")
            view_cmd = [
                samtools,
                "view",
                "-bh",
            ]
            if args.input.lower().endswith(".cram"):
                view_cmd.extend(["-T", ref])

            view_cmd.extend(
                [
                    args.input,
                    args.region,
                    "-o",
                    temp_bam,
                ]
            )
            run_command(view_cmd)
            run_command([samtools, "index", temp_bam])
            input_file = temp_bam
        except Exception as e:
            logging.error(f"Failed to extract region: {e}")
            if os.path.exists(temp_bam):
                os.remove(temp_bam)
            raise WGSExtractError("CNV region extraction failed.") from e

    try:
        # delly cnv -g ref.fa -o cnv.bcf input.bam
        delly = get_tool_path("delly")
        bcftools = get_tool_path("bcftools")
        cmd = [delly, "cnv", "-g", ref, "-o", out_bcf] + map_args + [input_file]
        run_command(cmd)
        # convert bcf to vcf.gz
        run_command([bcftools, "view", "-Oz", "-o", out_vcf, out_bcf])
        ensure_vcf_indexed(out_vcf)
        if os.path.exists(out_bcf):
            os.remove(out_bcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"CNV calling failed: {e}")
        if e.returncode < 0:
            import signal

            if abs(e.returncode) == signal.SIGSEGV:
                logging.error("❌: Delly segfaulted (Segmentation Fault).")
                logging.error(
                    "     This is common on macOS due to incompatible shared libraries (boost/htslib) in some environments."
                )
                logging.error(
                    "     Hint: Try 'brew install delly' or run in a Linux container."
                )
        else:
            logging.error(
                "Hint: If using macOS, ensure 'delly' and 'boost' are correctly installed via Homebrew."
            )
        raise WGSExtractError(
            f"CNV calling failed with exit code {e.returncode}"
        ) from e
    finally:
        if temp_bam and os.path.exists(temp_bam):
            os.remove(temp_bam)
            if os.path.exists(temp_bam + ".bai"):
                os.remove(temp_bam + ".bai")


def _ensure_fasta_index(ref: str) -> None:
    if os.path.isfile(ref + ".fai"):
        return
    samtools = get_tool_path("samtools")
    run_command([samtools, "faidx", ref])


def _prepare_pbsv_reference(ref: str, outdir: str) -> str:
    """Return a plain FASTA path because pbsv call does not accept .fa.gz input."""
    if not ref.lower().endswith(".gz"):
        return ref

    sibling = ref[:-3]
    if os.path.isfile(sibling):
        _ensure_fasta_index(sibling)
        return sibling

    os.makedirs(outdir, exist_ok=True)
    target = os.path.join(outdir, os.path.basename(sibling))
    if not os.path.isfile(target):
        logging.info(f"Creating uncompressed reference for pbsv: {target}")
        with gzip.open(ref, "rb") as source, open(target, "wb") as destination:
            shutil.copyfileobj(source, destination)
    _ensure_fasta_index(target)
    return target


def cmd_sv_pbsv(args):
    verify_dependencies(["pbsv", "bcftools", "tabix", "samtools"])
    log_dependency_info(["pbsv", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base
    pbsv_ref = _prepare_pbsv_reference(ref, outdir)

    out_vcf = os.path.join(outdir, "pbsv.vcf.gz")
    svsig = os.path.join(outdir, "pbsv.svsig.gz")
    raw_vcf = os.path.join(outdir, "pbsv.vcf")
    region = getattr(args, "region", None)
    tandem_repeats = getattr(args, "tandem_repeats", None)

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))
    try:
        pbsv = get_tool_path("pbsv")
        discover_cmd = [pbsv, "discover"]
        if region:
            discover_cmd.extend(["--region", region])
        if tandem_repeats:
            if not verify_paths_exist({"--tandem-repeats": tandem_repeats}):
                return
            discover_cmd.extend(["--tandem-repeats", tandem_repeats])
        if getattr(args, "ccs", False):
            discover_cmd.append("--hifi")
        discover_cmd.extend([args.input, svsig])
        run_command(discover_cmd)

        call_cmd = [pbsv, "call", "-j", threads]
        if region:
            call_cmd.extend(["--region", region])
        if getattr(args, "ccs", False):
            call_cmd.append("--ccs")
        call_cmd.extend([pbsv_ref, svsig, raw_vcf])
        run_command(call_cmd)

        bcftools = get_tool_path("bcftools")
        run_command([bcftools, "view", "-Oz", "-o", out_vcf, raw_vcf], check=True)
        ensure_vcf_indexed(out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"PacBio SV calling failed: {e}")
        raise WGSExtractError(
            f"PacBio SV calling failed with exit code {e.returncode}"
        ) from e


def cmd_sv_sniffles(args):
    verify_dependencies(["sniffles", "bcftools", "tabix", "samtools"])
    log_dependency_info(["sniffles", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, _ref, _lib = base

    raw_vcf = os.path.join(outdir, "sniffles.vcf")
    out_vcf = os.path.join(outdir, "sniffles.vcf.gz")
    region = getattr(args, "region", None)

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))
    try:
        sniffles = get_tool_path("sniffles")
        cmd = [
            sniffles,
            "--input",
            args.input,
            "--vcf",
            raw_vcf,
            "--threads",
            threads,
        ]
        if region:
            cmd.extend(["--regions", region])
        run_command(cmd)

        bcftools = get_tool_path("bcftools")
        run_command([bcftools, "view", "-Oz", "-o", out_vcf, raw_vcf], check=True)
        ensure_vcf_indexed(out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"Sniffles SV calling failed: {e}")
        raise WGSExtractError(
            f"Sniffles SV calling failed with exit code {e.returncode}"
        ) from e


def cmd_sv(args):
    if getattr(args, "pacbio", False):
        args.caller = "pbsv" if get_tool_path("pbsv") is not None else "sniffles"
        args.ccs = True
    if getattr(args, "caller", "delly") == "pbsv":
        cmd_sv_pbsv(args)
        return
    if getattr(args, "caller", "delly") == "sniffles":
        cmd_sv_sniffles(args)
        return

    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base

    out_bcf = os.path.join(outdir, "sv.bcf")
    out_vcf = os.path.join(outdir, "sv.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))

    temp_bam = None
    input_file = args.input

    if getattr(args, "region", None):
        import tempfile

        fd, temp_bam = tempfile.mkstemp(suffix=".bam", dir=outdir)
        os.close(fd)
        logging.info(f"Extracting region {args.region} to temporary BAM...")
        try:
            samtools = get_tool_path("samtools")
            view_cmd = [
                samtools,
                "view",
                "-bh",
            ]
            if args.input.lower().endswith(".cram"):
                view_cmd.extend(["-T", ref])

            view_cmd.extend(
                [
                    args.input,
                    args.region,
                    "-o",
                    temp_bam,
                ]
            )
            run_command(view_cmd)
            run_command([samtools, "index", temp_bam])
            input_file = temp_bam
        except Exception as e:
            logging.error(f"Failed to extract region: {e}")
            if os.path.exists(temp_bam):
                os.remove(temp_bam)
            raise WGSExtractError("SV region extraction failed.") from e

    try:
        # delly call -g ref.fa -o sv.bcf input.bam
        delly = get_tool_path("delly")
        bcftools = get_tool_path("bcftools")
        cmd = [delly, "call", "-g", ref, "-o", out_bcf, input_file]
        run_command(cmd)
        # convert bcf to vcf.gz
        run_command([bcftools, "view", "-Oz", "-o", out_vcf, out_bcf])
        ensure_vcf_indexed(out_vcf)
        if os.path.exists(out_bcf):
            os.remove(out_bcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"SV calling failed: {e}")
        logging.error(
            "Hint: If using macOS, ensure 'delly' and 'boost' are correctly installed via Homebrew."
        )
        raise WGSExtractError(f"SV calling failed with exit code {e.returncode}") from e
    finally:
        if temp_bam and os.path.exists(temp_bam):
            os.remove(temp_bam)
            if os.path.exists(temp_bam + ".bai"):
                os.remove(temp_bam + ".bai")


def _exit_if_missing(file_path, message_key, ann_name=None):
    if not file_path:
        msg = LOG_MESSAGES.get(message_key, f"Missing data file for {ann_name}")
        logging.error(f"REQUIRED DATA MISSING: {msg}")
        logging.info(
            f"To fix this, run: wgsextract ref {ann_name or message_key.split('_')[1]}"
        )
        sys.exit(2)  # Use exit code 2 to indicate missing data
