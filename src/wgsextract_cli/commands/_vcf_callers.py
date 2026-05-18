import logging
import os
import subprocess
import sys

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    popen,
)

from ._vcf_basic import (
    get_base_args,
)


def cmd_freebayes(args):
    verify_dependencies(["freebayes", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base

    out_vcf = os.path.join(outdir, "freebayes.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_freebayes"].format(output=out_vcf))
    region_args = ["-r", args.region] if args.region else []

    # Freebayes requires an uncompressed reference sequence.
    # If the reference is .gz, we must decompress it temporarily.
    temp_ref = None
    use_ref = ref

    if ref.lower().endswith(".gz"):
        logging.info(
            "Freebayes requires an uncompressed reference. Decompressing temporarily..."
        )
        import tempfile

        # Create temp file in outdir to ensure enough space
        fd, temp_ref = tempfile.mkstemp(suffix=".fa", dir=outdir)
        os.close(fd)
        try:
            with open(temp_ref, "wb") as f_out:
                run_command(["gunzip", "-c", ref], stdout=f_out, check=True)
            # Index the temp ref
            logging.info("Indexing temporary reference...")
            run_command(["samtools", "faidx", temp_ref], check=True)
            use_ref = temp_ref
        except Exception as e:
            logging.error(f"Failed to prepare uncompressed reference: {e}")
            if temp_ref and os.path.exists(temp_ref):
                os.remove(temp_ref)
                if os.path.exists(temp_ref + ".fai"):
                    os.remove(temp_ref + ".fai")
            return

    # Check if input is CRAM
    is_cram = args.input.lower().endswith(".cram")

    try:
        freebayes = get_tool_path("freebayes")
        bcftools = get_tool_path("bcftools")
        samtools = get_tool_path("samtools")

        if is_cram:
            # freebayes doesn't always handle CRAM perfectly via stdin
            view_cmd = [samtools, "view", "-uh", "-T", use_ref, args.input]
            if args.region:
                view_cmd.extend(
                    ["-r", args.region] if "-r" not in region_args else region_args
                )

            p_view = popen(view_cmd, stdout=subprocess.PIPE)
            p_fb = popen(
                [freebayes, "-f", use_ref, "--stdin"],
                stdin=p_view.stdout,
                stdout=subprocess.PIPE,
            )
            p_vcf = popen([bcftools, "view", "-Oz", "-o", out_vcf], stdin=p_fb.stdout)

            if p_view.stdout:
                p_view.stdout.close()
            if p_fb.stdout:
                p_fb.stdout.close()
            _, stderr = p_vcf.communicate()
            fb_returncode = p_fb.wait()
            view_returncode = p_view.wait()

            if view_returncode != 0 or fb_returncode != 0 or p_vcf.returncode != 0:
                logging.error(
                    "Freebayes pipeline failed with return codes "
                    f"samtools={view_returncode}, freebayes={fb_returncode}, "
                    f"bcftools={p_vcf.returncode}"
                )
                if stderr:
                    logging.error(stderr.decode(errors="replace"))
                raise WGSExtractError("Freebayes pipeline failed.")
        else:
            # BAM handling
            p1 = popen(
                [freebayes, "-f", use_ref] + region_args + [args.input],
                stdout=subprocess.PIPE,
            )
            p2 = popen(
                [bcftools, "view", "-Oz", "-o", out_vcf],
                stdin=p1.stdout,
                stderr=subprocess.PIPE,
            )
            if p1.stdout:
                p1.stdout.close()
            _, stderr = p2.communicate()
            freebayes_returncode = p1.wait()

            if freebayes_returncode != 0 or p2.returncode != 0:
                logging.error(
                    "Freebayes/bcftools failed with return codes "
                    f"freebayes={freebayes_returncode}, bcftools={p2.returncode}"
                )
                if stderr:
                    logging.error(stderr.decode(errors="replace"))
                raise WGSExtractError("Freebayes pipeline failed.")

        ensure_vcf_indexed(out_vcf)
    except Exception as e:
        logging.error(f"Freebayes failed: {e}")
        raise WGSExtractError("Freebayes failed.") from e
    finally:
        # Clean up temp reference
        if temp_ref and os.path.exists(temp_ref):
            logging.info("Cleaning up temporary reference...")
            os.remove(temp_ref)
            if os.path.exists(temp_ref + ".fai"):
                os.remove(temp_ref + ".fai")


def cmd_gatk(args):
    verify_dependencies(["gatk", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base

    out_vcf = os.path.join(outdir, "gatk.vcf.gz")

    # GATK requires a .dict file
    if not lib.dict_file:
        logging.info(LOG_MESSAGES["vcf_generating_dict"])
        dict_file = (
            ref.replace(".fa.gz", ".dict")
            .replace(".fasta.gz", ".dict")
            .replace(".fa", ".dict")
            .replace(".fasta", ".dict")
        )
        try:
            run_command(["samtools", "dict", "-o", dict_file, ref], check=True)
            lib.dict_file = dict_file
        except Exception as e:
            logging.error(f"Failed to generate .dict file: {e}")
            return

    logging.info(LOG_MESSAGES["vcf_calling_gatk"].format(output=out_vcf))
    region_args = ["-L", args.region] if args.region else []

    try:
        from wgsextract_cli.core.dependencies import get_tool_path

        gatk_tool = get_tool_path("gatk")
        # Use system gatk binary
        cmd = [
            gatk_tool,
            "HaplotypeCaller",
            "-R",
            ref,
            "-I",
            args.input,
            "-O",
            out_vcf,
        ] + region_args
        run_command(cmd)
        ensure_vcf_indexed(out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"GATK failed: {e}")
        if args.input.lower().endswith(".cram"):
            logging.error(
                "Hint: Older GATK versions do not support CRAM version 3.1. "
                "If this failed with a CRAM error, please convert to BAM or upgrade GATK."
            )
        sys.exit(e.returncode)
