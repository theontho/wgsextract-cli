import argparse
import copy
import csv
import logging
import os
from datetime import datetime
from typing import TypedDict

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import verify_paths_exist

from ._analyze_workflows import (
    _print,
    detect_vcf_type,
    generate_summary_report,
    run_bam_chain,
    run_cli_subcommand,
    run_vcf_workflow,
)


class BatchSample(TypedDict):
    input: str | None
    vcfs: list[str]


def cmd_comprehensive(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.config import settings

    verify_dependencies(["bcftools", "samtools", "tabix"])
    log_dependency_info(["bcftools", "samtools", "tabix"])

    batch_file = args.batch if args.batch else settings.get("batch_file_path")
    if batch_file:
        run_batch_comprehensive(args, batch_file)
        return

    input_file = args.input
    vcf_inputs = args.vcf_inputs if args.vcf_inputs else []
    env_vcf = settings.get("vcf_input_paths")
    if not vcf_inputs and env_vcf:
        if isinstance(env_vcf, list):
            vcf_inputs = env_vcf
        else:
            vcf_inputs = env_vcf.split()

    if not input_file and not vcf_inputs:
        raise WGSExtractError(LOG_MESSAGES["input_required"])

    # Pre-flight Validation
    paths_to_verify = {}
    if input_file:
        paths_to_verify["--input"] = input_file
    for i, v in enumerate(vcf_inputs):
        paths_to_verify[f"vcf-input-{i}"] = v

    if not verify_paths_exist(paths_to_verify):
        raise WGSExtractError("Input path verification failed.")

    outdir = args.outdir if args.outdir else os.getcwd()
    os.makedirs(outdir, exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _print(f"\nSTAGE: Starting Comprehensive Analysis ({now})")
    _print(f"Output Directory: {outdir}")

    _print("-" * 60)

    # 1. BAM/CRAM Analysis (Info, QC, Lineage)
    if input_file:
        run_bam_chain(args, input_file, outdir)

    # 2. VCF Processing (Independently per type)
    results = []
    if vcf_inputs:
        _print(f"\nSTAGE: Processing Input VCFs ({len(vcf_inputs)} files)")
        for vcf in vcf_inputs:
            v_type = detect_vcf_type(vcf)
            res = run_vcf_workflow(args, vcf, v_type, outdir)
            if res:
                results.append(res)
    elif input_file and not args.skip_calling:
        _print("\nSTAGE: Variant Calling (No input VCFs provided)")
        # Call SNV and InDels if nothing else provided
        snps_vcf = os.path.join(outdir, "snps.vcf.gz")
        run_cli_subcommand(
            ["vcf", "snp", "--input", input_file, "--outdir", outdir], args
        )

        indels_vcf = os.path.join(outdir, "indels.vcf.gz")
        run_cli_subcommand(
            ["vcf", "indel", "--input", input_file, "--outdir", outdir], args
        )

        for vcf, v_type in [(snps_vcf, "snp-indel"), (indels_vcf, "snp-indel")]:
            if os.path.exists(vcf):
                res = run_vcf_workflow(args, vcf, v_type, outdir)
                if res:
                    results.append(res)

    # 3. Final Summary Report
    generate_summary_report(results, outdir)


def run_batch_comprehensive(args: argparse.Namespace, batch_file: str) -> None:
    """Runs comprehensive analysis for multiple samples defined in batch_file."""

    if not os.path.exists(batch_file):
        raise WGSExtractError(f"Batch file not found: {batch_file}")

    with open(batch_file) as f:
        # Detect delimiter (CSV or TSV)
        dialect = csv.Sniffer().sniff(f.read(1024))
        f.seek(0)
        reader = csv.DictReader(f, dialect=dialect)

        # Map header names flexibly
        headers = reader.fieldnames or []
        name_col = next(
            (c for c in headers if c.lower() in ["name", "sample", "id"]), "name"
        )
        input_col = next(
            (c for c in headers if c.lower() in ["input", "bam", "cram"]), "input"
        )
        vcf_col = next((c for c in headers if "vcf" in c.lower()), "vcf")

        failed_samples: list[str] = []

        for row in reader:
            name = row.get(name_col) or "Unknown"
            input_path = row.get(input_col)
            vcf_paths = row.get(vcf_col) or ""

            # Clean up vcf_paths - handle comma or space separated
            vcf_list = []
            if vcf_paths:
                vcf_list = [
                    v.strip()
                    for v in vcf_paths.replace(",", ";").split(";")
                    if v.strip()
                ]

            _print("\n" + "#" * 80)
            _print(f"### BATCH PROCESSING: {name}")
            _print("#" * 80)

            # Create sample-specific outdir
            sample_outdir = os.path.join(
                args.outdir if args.outdir else os.getcwd(), name
            )
            os.makedirs(sample_outdir, exist_ok=True)

            # Build dummy args for this sample
            sample_args = copy.deepcopy(args)
            sample_args.input = input_path
            sample_args.vcf_inputs = vcf_list
            sample_args.outdir = sample_outdir
            sample_args.batch = None  # Prevent recursion

            try:
                cmd_comprehensive(sample_args)
            except (OSError, WGSExtractError) as e:
                logging.error(f"Failed to process sample {name}: {e}")
                failed_samples.append(name)

        if failed_samples:
            raise WGSExtractError(
                "Batch analysis failed for sample(s): " + ", ".join(failed_samples)
            )


def cmd_batch_gen(args: argparse.Namespace) -> None:
    """Generates a batch file by scanning a directory."""
    scan_dir = args.directory
    if not os.path.exists(scan_dir):
        raise WGSExtractError(f"Directory not found: {scan_dir}")

    _print(f"Scanning directory: {scan_dir}")

    samples: dict[str, BatchSample] = {}

    for root, _, files in os.walk(scan_dir):
        for f in files:
            full_path = os.path.abspath(os.path.join(root, f))
            lower_f = f.lower()

            # Heuristic: sample1.bam -> name sample1
            # sample1.vcf.gz -> name sample1
            # Strip common suffixes for better grouping
            def clean_name(n: str) -> str:
                # Strip all extensions first
                name = n
                while "." in name:
                    base, ext = os.path.splitext(name)
                    if ext.lower() in [
                        ".vcf",
                        ".gz",
                        ".bam",
                        ".cram",
                        ".tbi",
                        ".csi",
                        ".bai",
                        ".crai",
                    ]:
                        name = base
                    else:
                        break

                # Strip common genomic suffixes
                for suffix in [
                    "_snp",
                    "_sv",
                    "_indel",
                    "_cnv",
                    ".snp",
                    ".sv",
                    ".indel",
                    ".cnv",
                    "_metrics",
                    "_info",
                ]:
                    if name.lower().endswith(suffix):
                        name = name[: -len(suffix)]

                return name

            if lower_f.endswith((".bam", ".cram")):
                name = clean_name(f)
                if name not in samples:
                    samples[name] = {"input": None, "vcfs": []}
                samples[name]["input"] = full_path

            elif lower_f.endswith((".vcf.gz", ".vcf")):
                # Avoid adding indexes as VCFs
                if not lower_f.endswith((".tbi", ".csi")):
                    name = clean_name(f)
                    if name not in samples:
                        samples[name] = {"input": None, "vcfs": []}
                    samples[name]["vcfs"].append(full_path)

    if not samples:
        logging.warning("No genomic files found in the directory.")
        return

    output_path = args.output
    _print(f"Writing batch file to: {output_path}")

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "input", "vcf"])
        for name, data in sorted(samples.items()):
            vcf_str = ";".join(data["vcfs"])
            writer.writerow([name, data["input"] or "", vcf_str])

    _print(f"Generated batch file with {len(samples)} samples.")


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "analyze", help=CLI_HELP.get("cmd_analyze", "Analyze genomic data.")
    )
    analyze_subs = parser.add_subparsers(dest="analyze_cmd", required=True)

    comp_parser = analyze_subs.add_parser(
        "comprehensive",
        parents=[base_parser],
        help=CLI_HELP.get("cmd_comprehensive", "Full-chain analysis."),
    )
    comp_parser.add_argument(
        "--vcf-inputs",
        nargs="+",
        help=CLI_HELP.get("arg_vcf_inputs", "Multiple VCF files."),
    )
    comp_parser.add_argument(
        "--skip-calling",
        action="store_true",
        help="Skip variant calling if --vcf-inputs is missing (fail instead).",
    )
    comp_parser.add_argument(
        "--batch",
        help=CLI_HELP.get("arg_batch", "Batch file for multiple samples."),
    )
    comp_parser.set_defaults(func=cmd_comprehensive)

    gen_batch_parser = analyze_subs.add_parser(
        "batch-gen",
        parents=[base_parser],
        help=CLI_HELP.get("cmd_batch_gen", "Generate a batch file from a directory."),
    )
    gen_batch_parser.add_argument(
        "--directory",
        required=True,
        help=CLI_HELP.get("arg_directory", "Directory to scan."),
    )
    gen_batch_parser.add_argument(
        "--output",
        default="batch.csv",
        help="Output batch file path.",
    )
    gen_batch_parser.set_defaults(func=cmd_batch_gen)
