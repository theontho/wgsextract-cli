import logging
import os
import subprocess

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.regions import get_vcf_chr_name
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_chr_name,
    popen,
    verify_paths_exist,
)

from ._lineage_ydna import (
    cmd_ydna,
)


def cmd_mtdna(args):
    if not args.input:
        logging.error(LOG_MESSAGES["input_required"])
        return
    if not verify_paths_exist({"--input": args.input}):
        return

    # Check dependencies only after cheap input validation so invalid invocations fail fast.
    if not args.haplogrep_path:
        verify_dependencies(["haplogrep", "bcftools"])
    log_dependency_info(["haplogrep", "bcftools"])

    haplogrep_path = args.haplogrep_path or get_tool_path("haplogrep")

    # If it's just a tool name, resolve it to a full path or pixi command first
    # so that verify_paths_exist doesn't fail on it.
    if (
        haplogrep_path
        and not os.path.isabs(haplogrep_path)
        and "/" not in haplogrep_path
    ):
        resolved = get_tool_path(haplogrep_path)
        if resolved:
            haplogrep_path = resolved

    if not haplogrep_path or not verify_paths_exist(
        {"--haplogrep-path": haplogrep_path}
    ):
        raise WGSExtractError("Haplogrep path missing.")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.input))
    )
    logging.debug(f"Input file: {os.path.abspath(args.input)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    out_file = os.path.join(outdir, "haplogrep_results.txt")

    input_file = args.input
    temp_vcf = None

    try:
        is_vcf = input_file.lower().endswith((".vcf", ".vcf.gz"))
        is_alignment = input_file.lower().endswith((".bam", ".cram"))

        if is_alignment:
            logging.info(
                "Input is alignment. Extracting mitochondrial variants first..."
            )
            temp_vcf = os.path.join(outdir, "temp_haplogrep_input.vcf.gz")

            threads, _ = get_resource_defaults(args.threads, None)
            md5_sig = calculate_bam_md5(input_file, None)
            lib = ReferenceLibrary(args.ref, md5_sig)
            if not lib.fasta:
                logging.error(
                    "Reference genome required to call mitochondrial variants."
                )
                return

            logging.debug(f"Resolved reference: {lib.fasta}")

            cram_opt = ["-T", lib.fasta] if input_file.lower().endswith(".cram") else []
            chr_m = get_chr_name(input_file, "MT", cram_opt)

            # Fast variant calling for MT region
            bcftools = get_tool_path("bcftools")
            p1 = popen(
                [
                    bcftools,
                    "mpileup",
                    "-Ou",
                    "-r",
                    chr_m,
                    "-f",
                    lib.fasta,
                    input_file,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            p2 = popen(
                [bcftools, "call", "-mv", "-Oz", "-o", temp_vcf],
                stdin=p1.stdout,
                stderr=subprocess.DEVNULL,
            )
            if p1.stdout:
                p1.stdout.close()
            p2.communicate()

            if p2.returncode != 0:
                logging.error("Failed to extract mitochondrial variants.")
                raise WGSExtractError("Failed to extract mitochondrial variants.")
            input_file = temp_vcf

        elif is_vcf:
            # Filter VCF to chrM to avoid Haplogrep errors on large VCFs
            chr_m = get_vcf_chr_name(input_file, "MT")
            logging.info(f"Filtering VCF to {chr_m} for Haplogrep...")
            temp_vcf = os.path.join(outdir, "temp_haplogrep_filtered.vcf.gz")

            bcftools = get_tool_path("bcftools")
            run_command(
                [bcftools, "view", "-r", chr_m, "-Oz", "-o", temp_vcf, input_file]
            )
            input_file = temp_vcf

        logging.info(LOG_MESSAGES["running_haplogrep"].format(input=input_file))
        # Check if it's a JAR or a wrapper
        if haplogrep_path.endswith(".jar"):
            verify_dependencies(["java"])
            java = get_tool_path("java")
            if not java:
                raise WGSExtractError("Java is required to run Haplogrep JAR.")
            cmd = [java, "-jar", haplogrep_path]
        else:
            # haplogrep_path might already be a 'pixi run' string
            import shlex

            cmd = shlex.split(haplogrep_path)

        run_command(
            cmd
            + [
                "classify",
                "--format",
                "vcf",
                "--in",
                input_file,
                "--out",
                out_file,
            ]
        )

        # Print results directly to terminal
        if os.path.exists(out_file):
            print("\n🧬 Haplogrep Lineage Result:")
            print("-" * 30)
            try:
                with open(out_file) as f:
                    lines = f.readlines()
                    if len(lines) > 1:
                        # Skip header if it exists and looks like it
                        header = lines[0].strip().split("\t")
                        data = lines[1].strip().split("\t")
                        for h, d in zip(header, data, strict=False):
                            if d:
                                print(f"{h:<15} : {d}")
                    elif len(lines) == 1:
                        print(lines[0].strip())
            except Exception as e:
                logging.debug(f"Failed to print Haplogrep results: {e}")
            print("-" * 30 + "\n")

    except Exception as e:
        if isinstance(e, WGSExtractError):
            raise
        raise WGSExtractError(f"Haplogrep failed: {e}") from e
    finally:
        if temp_vcf and os.path.exists(temp_vcf):
            try:
                os.remove(temp_vcf)
                if os.path.exists(temp_vcf + ".tbi"):
                    os.remove(temp_vcf + ".tbi")
            except Exception:
                pass


def register(subparsers, base_parser):
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
