"""Pet Analysis command for aligning and calling variants in non-human species."""

import argparse
import logging
import subprocess
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import verify_paths_exist

from ._extract_helpers import cleanup_files
from .align import (
    duplicate_marker,
    ensure_bwa_index,
    reads_stem,
    run_streamed_alignment,
)

_SPECIES_REFERENCES = {
    "dog": "GCF_011100685.1_UU_Cfam_GSD_1.0_genomic.fna.gz",
    "cat": "GCF_018350175.1_F.catus_Fca126_mat1.0_genomic.fna.gz",
}


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "pet-align", parents=[base_parser], help=CLI_HELP["cmd_pet-align"]
    )
    parser.add_argument("--r1", help=CLI_HELP["arg_r1"])
    parser.add_argument("--r2", help=CLI_HELP["arg_r2"])
    parser.add_argument(
        "--species", choices=["dog", "cat"], required=True, help="Species for analysis"
    )
    parser.add_argument(
        "--format", choices=["BAM", "CRAM"], default="BAM", help="Output format"
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    verify_dependencies(["bwa", "samtools", "bcftools"])
    log_dependency_info(["bwa", "samtools", "bcftools"])

    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves FASTQ inputs.")
    paths_to_check = {"--r1": args.r1}
    if args.r2:
        paths_to_check["--r2"] = args.r2
    verify_paths_exist(paths_to_check)

    threads, memory = get_resource_defaults(args.threads, args.memory)

    if not args.ref:
        raise WGSExtractError(LOG_MESSAGES["ref_required"])
    if Path(args.ref).is_file():
        ref_file = args.ref
    else:
        ref_file = str(Path(args.ref, "genomes", _SPECIES_REFERENCES[args.species]))
    logging.debug(f"Resolved reference: {ref_file}")
    if not Path(ref_file).exists():
        hint = (
            ""
            if Path(args.ref).is_file()
            else " Download it with `wgsextract ref library --install`."
        )
        raise WGSExtractError(
            f"Reference genome for {args.species} not found at {ref_file}.{hint}"
        )
    ensure_bwa_index(ref_file)

    outdir = args.outdir or str(absolute_path(args.r1).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    base_name = f"{reads_stem(args.r1)}_{args.species}"
    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_bam = str(Path(outdir, base_name + ext))
    out_vcf = str(Path(outdir, f"{base_name}.vcf.gz"))

    logging.info(LOG_MESSAGES["pet_aligning"].format(species=args.species))
    align_cmd = ["bwa", "mem", "-t", threads, ref_file, args.r1]
    try:
        run_streamed_alignment(
            align_cmd + ([args.r2] if args.r2 else []),
            out_bam,
            threads=threads,
            memory=memory,
            fmt=args.format,
            ref=ref_file,
            duplicates=duplicate_marker(paired=bool(args.r2)),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Alignment error: {e}") from e

    logging.info(LOG_MESSAGES["pet_calling"])
    try:
        with cleanup_files(on_failure=[out_vcf, out_vcf + ".csi"]):
            run_pipeline(
                [
                    ["bcftools", "mpileup", "--threads", threads, "-Ou", "-f", ref_file]
                    + [out_bam],
                    ["bcftools", "call", "--threads", threads, "-mv", "-Oz"]
                    + ["-o", out_vcf],
                ],
                description="Variant calling",
            )
            logging.info("Indexing VCF...")
            run_command(["bcftools", "index", "-f", out_vcf])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Variant calling error: {e}") from e

    logging.info(LOG_MESSAGES["pet_complete"])
    logging.info(LOG_MESSAGES["pet_results"].format(bam=out_bam, vcf=out_vcf))
