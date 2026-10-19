import argparse
import logging
import os
import shutil
import subprocess

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_chr_name,
    verify_paths_exist,
)

from ._lineage_ydna import (
    VCF_SUFFIXES,
    cmd_ydna,
    extract_vcf_chromosome,
    resolve_lineage_tool,
    tool_command,
)


def _haplogrep_command(haplogrep_path: str) -> list[str]:
    """How to run Haplogrep. Bioconda's haplogrep 2.4 wrapper passes its own
    path to java unquoted, so it fails whenever the install path has a space
    (the GUI's runtime lives in "Application Support"); run its jar instead."""
    jar = haplogrep_path
    if not jar.endswith(".jar"):
        resolved = shutil.which(haplogrep_path) or haplogrep_path
        sibling = os.path.join(
            os.path.dirname(os.path.realpath(resolved)), "haplogrep.jar"
        )
        if not os.path.isfile(sibling):
            return tool_command(haplogrep_path)
        jar = sibling
    verify_dependencies(["java"])
    return ["java", "-jar", jar]


def cmd_mtdna(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})

    logging.info("Running mtDNA haplogroup analysis...")

    # Check dependencies only after cheap input validation so invalid invocations fail fast.
    haplogrep_path = resolve_lineage_tool(
        args.haplogrep_path, "haplogrep", "--haplogrep-path"
    )
    verify_dependencies(["bcftools"])
    log_dependency_info(["haplogrep", "bcftools"])

    cmd = _haplogrep_command(haplogrep_path)

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input))
    os.makedirs(outdir, exist_ok=True)
    logging.debug(f"Input file: {os.path.abspath(args.input)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    out_file = os.path.join(outdir, "haplogrep_results.txt")

    input_file = args.input
    input_lower = input_file.lower()
    temp_vcf = os.path.join(outdir, "temp_haplogrep_input.vcf.gz")

    try:
        if input_lower.endswith((".bam", ".cram")):
            logging.info(
                "Input is alignment. Extracting mitochondrial variants first..."
            )
            lib = ReferenceLibrary(
                args.ref, calculate_bam_md5(input_file, None), input_path=input_file
            )
            if not lib.fasta:
                raise WGSExtractError(
                    "Reference genome required to call mitochondrial variants; pass --ref."
                )
            logging.debug(f"Resolved reference: {lib.fasta}")

            cram_opt = ["-T", lib.fasta] if input_lower.endswith(".cram") else []
            chr_m = get_chr_name(input_file, "MT", cram_opt)
            run_pipeline(
                [
                    ["bcftools", "mpileup", "-Ou", "-r", chr_m, "-f", lib.fasta]
                    + [input_file],
                    ["bcftools", "call", "--ploidy", "1", "-mv", "-Oz", "-o"]
                    + [temp_vcf],
                ],
                stdout=subprocess.DEVNULL,
                description="Mitochondrial variant calling",
            )
            input_file = temp_vcf
        elif input_lower.endswith(VCF_SUFFIXES):
            # Filter VCF to chrM to avoid Haplogrep errors on large VCFs
            extract_vcf_chromosome(input_file, "MT", temp_vcf)
            input_file = temp_vcf

        logging.info(LOG_MESSAGES["running_haplogrep"].format(input=input_file))
        try:
            run_command(
                cmd
                + ["classify", "--format", "vcf", "--in", input_file]
                + ["--out", out_file]
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Haplogrep failed: {e}") from e
    finally:
        for path in (temp_vcf, temp_vcf + ".tbi"):
            if os.path.exists(path):
                os.remove(path)

    _print_haplogrep_result(out_file)


def _print_haplogrep_result(out_file: str) -> None:
    try:
        with open(out_file) as f:
            lines = f.readlines()
    except OSError as e:
        logging.warning(f"Could not read Haplogrep results {out_file}: {e}")
        return
    print("\n🧬 Haplogrep Lineage Result:")
    print("-" * 30)
    if len(lines) > 1:
        header = lines[0].strip().split("\t")
        data = lines[1].strip().split("\t")
        for h, d in zip(header, data, strict=False):
            if d:
                print(f"{h:<15} : {d}")
    elif lines:
        print(lines[0].strip())
    print("-" * 30 + "\n")


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("lineage", help="Executes Yleaf or Haplogrep.")
    lin_subs = parser.add_subparsers(dest="lin_cmd", required=True)

    ydna_parser = lin_subs.add_parser(
        "y-haplogroup", parents=[base_parser], help=CLI_HELP["cmd_lineage-y-haplogroup"]
    )
    ydna_parser.add_argument(
        "--yleaf-path",
        default=settings.get("yleaf_executable"),
        help="Path to yleaf.py (optional if in PATH)",
    )
    ydna_parser.add_argument("--pos-file", help="Yleaf position file (optional)")
    ydna_parser.add_argument(
        "--extra-args", help="Extra arguments to pass to Yleaf (e.g. -force)"
    )
    ydna_parser.set_defaults(func=cmd_ydna)

    mtdna_parser = lin_subs.add_parser(
        "mt-haplogroup",
        parents=[base_parser],
        help=CLI_HELP["cmd_lineage-mt-haplogroup"],
    )
    mtdna_parser.add_argument(
        "--haplogrep-path",
        default=settings.get("haplogrep_executable"),
        help="Path to haplogrep executable or JAR (optional if in PATH)",
    )
    mtdna_parser.set_defaults(func=cmd_mtdna)
