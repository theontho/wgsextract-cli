import argparse
import logging
import os
import subprocess

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    ensure_vcf_prepared,
    popen,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import print_warning


def _select_vcf_input(args: argparse.Namespace) -> str | None:
    input_path = getattr(args, "input", None)
    vcf_input = getattr(args, "vcf_input", None)
    default_vcf = settings.get("default_input_vcf")
    explicit_dests: set[str] = getattr(args, "_explicit_dests", set())

    if vcf_input and vcf_input != default_vcf:
        return str(vcf_input)
    if "input" in explicit_dests and input_path:
        return str(input_path)
    selected = vcf_input if vcf_input else input_path
    return str(selected) if selected else None


def get_base_args(
    args: argparse.Namespace,
) -> tuple[str, str, str, ReferenceLibrary] | None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])

    # Support multiple input argument names for different VCF commands
    input_file = (
        getattr(args, "vcf_input", None)
        or getattr(args, "input", None)
        or getattr(args, "proband", None)
    )

    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        return None
    args.input = input_file

    logging.debug(f"Input file: {os.path.abspath(input_file)}")

    threads, _ = get_resource_defaults(args.threads, None)

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    md5_sig = calculate_bam_md5(input_file, None)
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)

    if not hasattr(args, "ploidy") or args.ploidy is None:
        if not hasattr(args, "ploidy_file") or args.ploidy_file is None:
            args.ploidy_file = lib.ploidy_file

    resolved_ref = lib.fasta
    logging.debug(f"Resolved reference: {resolved_ref}")

    paths_to_check = {"--input": input_file}
    if resolved_ref:
        paths_to_check["--ref"] = resolved_ref

    if not verify_paths_exist(paths_to_check):
        return None

    if not resolved_ref or not os.path.isfile(resolved_ref):
        logging.error(LOG_MESSAGES["ref_required_for"].format(task="variant calling"))
        return None
    return threads, outdir, resolved_ref, lib


def cmd_snp(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    base = get_base_args(args)
    if not base:
        raise WGSExtractError("Failed to resolve base arguments for SNP calling.")

    threads, outdir, ref, lib = base

    print_warning("ButtonSNPVCF", threads=threads)

    out_vcf = os.path.join(outdir, "snps.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_snps"].format(output=out_vcf))
    region_args = ["-r", args.region] if args.region else []

    ploidy_args = []
    if args.ploidy_file:
        if not verify_paths_exist({"--ploidy-file": args.ploidy_file}):
            raise WGSExtractError("Invalid --ploidy-file path.")
        ploidy_args = ["--ploidy-file", args.ploidy_file]
    elif args.ploidy:
        ploidy_args = ["--ploidy", args.ploidy]
    else:
        logging.error(
            "Ploidy (--ploidy or --ploidy-file) is required and could not be auto-resolved from --ref."
        )
        raise WGSExtractError("Ploidy resolution failed.")

    bcftools = get_tool_path("bcftools")
    if bcftools is None:
        raise WGSExtractError("bcftools dependency is required for SNP calling.")
    p1 = popen(
        [bcftools, "mpileup", "-B", "-I", "-C", "50", "-f", ref, "-Ou"]
        + region_args
        + [args.input],
        stdout=subprocess.PIPE,
    )
    p2 = popen(
        [bcftools, "call"]
        + ploidy_args
        + [
            "-V",
            "indels",
            "-v",
            "-m",
            "-P",
            "0",
            "--threads",
            threads,
            "-Oz",
            "-o",
            out_vcf,
        ],
        stdin=p1.stdout,
        stderr=subprocess.PIPE,
    )
    if p1.stdout:
        p1.stdout.close()
    _, stderr = p2.communicate()
    mpileup_returncode = p1.wait()

    if mpileup_returncode != 0 or p2.returncode != 0:
        logging.error(f"bcftools call failed with return code {p2.returncode}")
        if mpileup_returncode != 0:
            logging.error(
                f"bcftools mpileup failed with return code {mpileup_returncode}"
            )
        if stderr:
            logging.error(stderr.decode(errors="replace"))
        raise WGSExtractError("SNP variant calling failed.")

    ensure_vcf_indexed(out_vcf)


def cmd_indel(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    base = get_base_args(args)
    if not base:
        raise WGSExtractError("Failed to resolve base arguments for InDel calling.")

    threads, outdir, ref, lib = base

    print_warning("ButtonInDelVCF", threads=threads)

    out_vcf = os.path.join(outdir, "indels.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_indels"].format(output=out_vcf))
    region_args = ["-r", args.region] if args.region else []

    ploidy_args = []
    if args.ploidy_file:
        if not verify_paths_exist({"--ploidy-file": args.ploidy_file}):
            raise WGSExtractError("Invalid --ploidy-file path.")
        ploidy_args = ["--ploidy-file", args.ploidy_file]
    elif args.ploidy:
        ploidy_args = ["--ploidy", args.ploidy]
    else:
        logging.error(
            "Ploidy (--ploidy or --ploidy-file) is required and could not be auto-resolved from --ref."
        )
        raise WGSExtractError("Ploidy resolution failed.")

    bcftools = get_tool_path("bcftools")
    if bcftools is None:
        raise WGSExtractError("bcftools dependency is required for InDel calling.")
    p1 = popen(
        [bcftools, "mpileup", "-B", "-C", "50", "-f", ref, "-Ou"]
        + region_args
        + [args.input],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    p2 = popen(
        [bcftools, "call"]
        + ploidy_args
        + ["-V", "snps", "-v", "-m", "-P", "0", "--threads", threads, "-Ou"],
        stdin=p1.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p1.stdout:
        p1.stdout.close()
    stdout, stderr = p2.communicate()
    mpileup_stderr = p1.stderr.read() if p1.stderr else None
    mpileup_returncode = p1.wait()

    if mpileup_returncode != 0 or p2.returncode != 0:
        logging.error(f"bcftools call failed with return code {p2.returncode}")
        if mpileup_returncode != 0:
            logging.error(
                f"bcftools mpileup failed with return code {mpileup_returncode}"
            )
        if mpileup_stderr:
            logging.error(mpileup_stderr.decode(errors="replace"))
        if stderr:
            logging.error(stderr.decode(errors="replace"))
        raise WGSExtractError("InDel variant calling failed.")

    p3 = popen(
        [bcftools, "norm", "-f", ref, "--threads", threads, "-Oz", "-o", out_vcf],
        stdin=subprocess.PIPE,
    )
    p3.communicate(input=stdout)

    if p3.returncode != 0:
        logging.error(f"bcftools norm failed with return code {p3.returncode}")
        raise WGSExtractError("InDel normalization failed.")

    ensure_vcf_indexed(out_vcf)


def cmd_annotate(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = args.input if args.input else args.vcf_input
    if not input_file:
        msg = LOG_MESSAGES["input_required"]
        logging.error(msg)
        raise WGSExtractError(msg)

    logging.debug(f"Input file: {os.path.abspath(input_file)}")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    out_vcf = os.path.join(outdir, "annotated.vcf.gz")

    ann_vcf = args.ann_vcf
    cols = args.cols

    if not ann_vcf:
        # Try to auto-resolve from reference library
        md5_sig = (
            calculate_bam_md5(input_file, None)
            if input_file.lower().endswith((".bam", ".cram"))
            else None
        )
        lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
        if lib.ref_vcf_tab:
            ann_vcf = lib.ref_vcf_tab
            logging.info(f"Auto-resolved annotation file: {ann_vcf}")
        else:
            logging.error("--ann-vcf is required and could not be auto-resolved.")
            raise WGSExtractError(
                "--ann-vcf is required and could not be auto-resolved."
            )

    if not cols:
        # Dynamically resolve columns from the annotation file
        if ann_vcf.lower().endswith(
            (".tab", ".tab.gz", ".txt", ".txt.gz", ".csv", ".csv.gz")
        ):
            try:
                import gzip

                open_func = gzip.open if ann_vcf.endswith(".gz") else open
                with open_func(ann_vcf, "rt") as f:
                    header = f.readline().strip()
                    if header.startswith("#"):
                        header = header[1:]
                    # Map common names to bcftools expected names
                    col_map = {
                        "CHROM": "CHROM",
                        "CHR": "CHROM",
                        "#CHROM": "CHROM",
                        "POS": "POS",
                        "ID": "ID",
                        "HG": "INFO/HG",
                        "RSID": "ID",
                    }
                    found_cols = []
                    for col_name in header.split():
                        upper_col = col_name.upper().lstrip("#")
                        if upper_col in col_map:
                            found_cols.append(col_map[upper_col])
                        else:
                            found_cols.append("-")  # Skip unknown columns

                    if "CHROM" in found_cols and "POS" in found_cols:
                        cols = ",".join(found_cols)
                        logging.info(f"Auto-resolved columns from header: {cols}")
            except (OSError, UnicodeError, ValueError):
                logging.debug("Failed to parse tab header.", exc_info=True)

        if not cols and ann_vcf.lower().endswith((".vcf", ".vcf.gz")):
            # For VCFs, default to ID and HG if present in header
            try:
                res = run_command(["bcftools", "view", "-h", ann_vcf])
                found_cols = ["ID"]
                if "ID=HG" in res.stdout:
                    found_cols.append("INFO/HG")
                cols = ",".join(found_cols)
                logging.info(f"Auto-resolved VCF columns: {cols}")
            except (OSError, subprocess.SubprocessError, RuntimeError, WGSExtractError):
                logging.debug(
                    "Failed to inspect VCF header for annotation columns.",
                    exc_info=True,
                )

    if not cols:
        # Fallback to a safe default if still not resolved
        cols = "ID"
        logging.info(f"Using default annotation column: {cols}")

    if not verify_paths_exist({"--input": input_file, "--ann-vcf": ann_vcf}):
        raise WGSExtractError("Annotation input path validation failed.")

    # Ensure inputs are bgzipped and indexed
    input_vcf = ensure_vcf_prepared(input_file)
    ann_vcf = ensure_vcf_prepared(ann_vcf)

    logging.info(LOG_MESSAGES["vcf_annotating"].format(input=input_vcf, output=out_vcf))
    try:
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                ann_vcf,
                "-c",
                cols,
                "-Oz",
                "-o",
                out_vcf,
                input_vcf,
            ]
        )
        ensure_vcf_indexed(out_vcf)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"❌: Annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from e
