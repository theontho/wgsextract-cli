import argparse
import logging
import os
import subprocess

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)

from ..vcf.basic import _select_vcf_input


def cmd_fastp(args: argparse.Namespace) -> None:
    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves FASTQ inputs.")

    from wgsextract_cli.core.variant_files import verify_paths_exist

    paths = {"--r1": args.r1}
    if args.r2:
        paths["--r2"] = args.r2
    verify_paths_exist(paths)

    verify_dependencies(["fastp"])
    log_dependency_info(["fastp"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.r1))

    logging.debug(f"Input file (R1): {os.path.abspath(args.r1)}")
    if args.r2:
        logging.debug(f"Input file (R2): {os.path.abspath(args.r2)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    os.makedirs(outdir, exist_ok=True)

    base_name = os.path.basename(args.r1).split(".")[0]
    out_r1 = os.path.join(outdir, f"{base_name}_fp_1.fastq.gz")
    out_json = os.path.join(outdir, f"{base_name}_fastp.json")
    out_html = os.path.join(outdir, f"{base_name}_fastp.html")

    cmd = [
        "fastp",
        "--thread",
        threads,
        "-i",
        args.r1,
        "-o",
        out_r1,
        "-j",
        out_json,
        "-h",
        out_html,
    ]
    if args.r2:
        out_r2 = os.path.join(outdir, f"{base_name}_fp_2.fastq.gz")
        cmd.extend(["-I", args.r2, "-O", out_r2])

    logging.info(LOG_MESSAGES["running_fastp"].format(input=args.r1))
    try:
        run_command(cmd)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"fastp failed: {e}") from e


def cmd_fastqc(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])

    from wgsextract_cli.core.variant_files import verify_paths_exist

    verify_paths_exist({"--input": args.input})

    verify_dependencies(["fastqc"])
    log_dependency_info(["fastqc"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.input))
    )

    logging.debug(f"Input file: {os.path.abspath(args.input)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    os.makedirs(outdir, exist_ok=True)

    logging.info(LOG_MESSAGES["running_fastqc"].format(input=args.input))
    try:
        run_command(["fastqc", "-t", threads, "-o", outdir, args.input])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"FastQC failed: {e}") from e


def cmd_vcf_qc(args: argparse.Namespace) -> None:
    input_file = _select_vcf_input(args)
    if not input_file:
        raise WGSExtractError("--input is required.")

    from wgsextract_cli.core.variant_files import verify_paths_exist

    verify_paths_exist({"--input": input_file})

    verify_dependencies(["bcftools"])
    log_dependency_info(["bcftools"])

    logging.debug(f"Input file: {os.path.abspath(input_file)}")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    os.makedirs(outdir, exist_ok=True)
    base_name = os.path.basename(input_file)
    out_stats = os.path.join(outdir, f"{base_name}.vcfstats.txt")

    logging.info(LOG_MESSAGES["vcf_stats"].format(input=input_file, output=out_stats))
    try:
        with open(out_stats, "w") as f:
            run_command(["bcftools", "stats", input_file], stdout=f)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"VCF stats failed: {e}") from e
