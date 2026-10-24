import argparse
import json
import logging
import subprocess
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genome_library import pair_stem
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)

from .._extract_helpers import cleanup_files
from ..align import reads_stem
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
    outdir = args.outdir if args.outdir else str(absolute_path(args.r1).parent)

    logging.debug(f"Input file (R1): {str(absolute_path(args.r1))}")
    if args.r2:
        logging.debug(f"Input file (R2): {str(absolute_path(args.r2))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    Path(outdir).mkdir(parents=True, exist_ok=True)

    base_name = fastp_stem(args.r1, args.r2)
    out_r1 = str(Path(outdir, f"{base_name}_fp_1.fastq.gz"))
    out_json = str(Path(outdir, f"{base_name}_fastp.json"))
    out_html = str(Path(outdir, f"{base_name}_fastp.html"))

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
        out_r2 = str(Path(outdir, f"{base_name}_fp_2.fastq.gz"))
        cmd.extend(["-I", args.r2, "-O", out_r2])

    logging.info(LOG_MESSAGES["running_fastp"].format(input=args.r1))
    outputs = [out_r1, out_json, out_html] + ([out_r2] if args.r2 else [])
    try:
        with cleanup_files(on_failure=outputs):
            run_command(cmd)
            # fastp exits 0 on a file that isn't FASTQ, reporting no reads.
            if _fastp_total_reads(out_json) == 0:
                raise WGSExtractError(
                    f"fastp found no reads in {args.r1}: is it a FASTQ file?"
                )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"fastp failed: {e}") from e


def fastp_stem(r1: str, r2: str | None) -> str:
    """The name qc fastp gives its outputs: the reads' stem, without the mate
    marker when both mates' outputs share it."""
    return pair_stem(reads_stem(r1)) if r2 else reads_stem(r1)


def _fastp_total_reads(report: str) -> int | None:
    try:
        with open(report) as f:
            return int(json.load(f)["summary"]["before_filtering"]["total_reads"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cmd_fastqc(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])

    from wgsextract_cli.core.variant_files import verify_paths_exist

    verify_paths_exist({"--input": args.input})

    verify_dependencies(["fastqc"])
    log_dependency_info(["fastqc"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else str(absolute_path(args.input).parent)

    logging.debug(f"Input file: {str(absolute_path(args.input))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    Path(outdir).mkdir(parents=True, exist_ok=True)

    logging.info(LOG_MESSAGES["running_fastqc"].format(input=args.input))
    try:
        run_command(["fastqc", "-t", threads, "--outdir", outdir, args.input])
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

    logging.debug(f"Input file: {str(absolute_path(input_file))}")

    outdir = args.outdir if args.outdir else str(absolute_path(input_file).parent)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    Path(outdir).mkdir(parents=True, exist_ok=True)
    base_name = Path(input_file).name
    out_stats = str(Path(outdir, f"{base_name}.vcfstats.txt"))

    logging.info(LOG_MESSAGES["vcf_stats"].format(input=input_file, output=out_stats))
    try:
        with cleanup_files(on_failure=[out_stats]), open(out_stats, "w") as f:
            run_command(["bcftools", "stats", input_file], stdout=f)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"VCF stats failed: {e}") from e
