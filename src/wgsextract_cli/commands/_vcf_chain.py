import argparse
import logging
import os
import shutil
import subprocess
import sys

from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_prepared,
)


def cmd_chain_annotate(args: argparse.Namespace) -> None:

    verify_dependencies(["bcftools", "tabix"])
    input_file = args.input if args.input else args.vcf_input
    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        raise WGSExtractError("VCF processing failed.")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    annotations = [a.strip().lower() for a in args.annotations.split(",") if a.strip()]

    if not annotations:
        logging.error("No valid annotations provided in --annotations.")
        return

    logging.info(f"Starting chained annotation with: {', '.join(annotations)}")

    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)

    current_input = ensure_vcf_prepared(input_file)
    intermediate_files = []
    finalized = False

    try:
        for i, ann in enumerate(annotations):
            step_outdir = os.path.join(outdir, f"chain_step_{i + 1}_{ann}")
            os.makedirs(step_outdir, exist_ok=True)

            # Print status directly to terminal so user sees progress during silenced runs
            print(f"  ➡️  Step [{i + 1}/{len(annotations)}]: Running '{ann}'...")

            # Pre-check tool availability
            from wgsextract_cli.core.dependencies import get_tool_path

            tool_to_check = (
                "vep" if ann == "vep" else "bcftools"
            )  # most others use bcftools
            if not get_tool_path(tool_to_check):
                logging.warning(
                    f"Skipping '{ann}': Tool '{tool_to_check}' not installed."
                )
                continue

            # Pre-check data availability
            missing_data = False
            if ann == "clinvar" and not lib.clinvar_vcf:
                missing_data = True
            elif ann == "revel" and not lib.revel_file:
                missing_data = True
            elif ann == "phylop" and not lib.phylop_file:
                missing_data = True
            elif ann == "gnomad" and not lib.gnomad_vcf:
                missing_data = True
            elif ann == "spliceai" and not lib.spliceai_vcf:
                missing_data = True
            elif ann == "alphamissense" and not lib.alphamissense_vcf:
                missing_data = True
            elif ann == "pharmgkb" and not lib.pharmgkb_vcf:
                missing_data = True
            elif ann == "vep" and not lib.vep_cache:
                # VEP can run in online mode, but for chain-annotate we generally want the cache
                # Only skip if offline cache is missing? The user mentioned "haven't downloaded due to size".
                # Let's just warn if cache is missing but maybe don't skip yet?
                # Actually, the user wants a clean one-line skipped message.
                missing_data = True

            if missing_data:
                logging.warning(
                    f"Skipping '{ann}': Required reference data not found. Run 'wgsextract ref {ann}' to download."
                )
                continue

            logging.info(f"[{i + 1}/{len(annotations)}] Running '{ann}' annotation...")

            cmd = [sys.executable, "-m", "wgsextract_cli.main"]

            if ann == "vep":
                cmd.extend(["vep", "run"])
            elif ann in [
                "clinvar",
                "revel",
                "phylop",
                "gnomad",
                "spliceai",
                "alphamissense",
                "pharmgkb",
            ]:
                cmd.extend(["vcf", ann])
            else:
                logging.warning(f"Unknown annotation type '{ann}', skipping.")
                continue

            cmd.extend(["--input", current_input, "--outdir", step_outdir])

            if args.ref:
                cmd.extend(["--ref", args.ref])

            try:
                # Capture output to prevent spam during chained annotation
                res = run_command(cmd, capture_output=True, check=True)
            except subprocess.CalledProcessError as e:
                logging.warning(f"Annotation step '{ann}' failed. Skipping.")
                if e.stderr:
                    for line in e.stderr.strip().split("\n"):
                        logging.error(f"  [{ann} ERROR] {line}")
                if e.stdout:
                    for line in e.stdout.strip().split("\n"):
                        logging.debug(f"  [{ann} STDOUT] {line}")
                # Cleanup step directory if it failed
                if os.path.exists(step_outdir):
                    shutil.rmtree(step_outdir, ignore_errors=True)
                continue

            # Find the output VCF from this step
            out_files = [
                f
                for f in os.listdir(step_outdir)
                if f.endswith(".vcf.gz")
                and not f.endswith(".norm.vcf.gz")
                and "gt_" not in f
            ]

            if not out_files:
                logging.warning(f"No VCF output found for step '{ann}'. Skipping.")
                if res.stderr:
                    for line in res.stderr.strip().split("\n"):
                        logging.warning(f"  [{ann} STDERR] {line}")
                if os.path.exists(step_outdir):
                    shutil.rmtree(step_outdir, ignore_errors=True)
                continue

            # Assume the newest VCF is the result
            out_files_paths = [os.path.join(step_outdir, f) for f in out_files]
            latest_out = max(out_files_paths, key=os.path.getmtime)

            intermediate_files.append(latest_out)
            current_input = latest_out
            logging.info(f"Step '{ann}' completed. Intermediate file: {latest_out}")

        # Finalize
        final_out = os.path.join(outdir, "chain_annotated.vcf.gz")

        shutil.copy2(current_input, final_out)
        if os.path.exists(current_input + ".tbi"):
            shutil.copy2(current_input + ".tbi", final_out + ".tbi")
        if os.path.exists(current_input + ".csi"):
            shutil.copy2(current_input + ".csi", final_out + ".csi")
        finalized = True

        logging.info(f"✅ Chain annotation complete: {final_out}")

    finally:
        if not getattr(args, "keep_intermediates", False):
            if finalized:
                logging.info("Cleaning up intermediate files...")
                for i, ann in enumerate(annotations):
                    step_outdir = os.path.join(outdir, f"chain_step_{i + 1}_{ann}")
                    if os.path.exists(step_outdir):
                        shutil.rmtree(step_outdir, ignore_errors=True)
            else:
                logging.warning(
                    "Preserving chain annotation intermediates because finalization did not complete."
                )
