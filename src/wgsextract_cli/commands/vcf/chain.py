import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from wgsextract_cli.core.config import config_args
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import calculate_bam_md5, ensure_vcf_prepared

from .._vep_run import vep_output_paths
from .basic import require_vcf_input
from .clinvar_revel import CLINVAR_ANNOTATED, REVEL_ANNOTATED
from .deleteriousness import CADD_ANNOTATED, GERP_ANNOTATED
from .population import GNOMAD_ANNOTATED, PHYLOP_ANNOTATED
from .splicing import ALPHAMISSENSE_ANNOTATED, PHARMGKB_ANNOTATED, SPLICEAI_ANNOTATED

# annotation -> (ReferenceLibrary attribute holding its data, step output name)
_VCF_STEPS = {
    "clinvar": ("clinvar_vcf", CLINVAR_ANNOTATED),
    "revel": ("revel_file", REVEL_ANNOTATED),
    "phylop": ("phylop_file", PHYLOP_ANNOTATED),
    "cadd": ("cadd_file", CADD_ANNOTATED),
    "gerp": ("gerp_file", GERP_ANNOTATED),
    "gnomad": ("gnomad_vcf", GNOMAD_ANNOTATED),
    "spliceai": ("spliceai_vcf", SPLICEAI_ANNOTATED),
    "alphamissense": ("alphamissense_vcf", ALPHAMISSENSE_ANNOTATED),
    "pharmgkb": ("pharmgkb_vcf", PHARMGKB_ANNOTATED),
}


def _step_command(ann: str) -> list[str]:
    if ann == "vep":
        return ["vep", "run"]
    return ["vcf", ann]


def _step_output(ann: str, step_input: str, step_outdir: str) -> str:
    if ann == "vep":
        return vep_output_paths(step_input, step_outdir, "vcf")[1]
    return str(Path(step_outdir, _VCF_STEPS[ann][1]))


def skip_reason(ann: str, lib: ReferenceLibrary) -> str | None:
    """Why an annotation step can't run here, or None if it can."""
    tool = "vep" if ann == "vep" else "bcftools"
    if not get_tool_path(tool):
        return f"{tool} is not installed."
    # chain-annotate is meant to run offline, so VEP also needs its cache.
    attr = "vep_cache" if ann == "vep" else _VCF_STEPS[ann][0]
    if not getattr(lib, attr, None):
        return (
            "required reference data not found. "
            f"Run 'wgsextract ref {ann}' to download."
        )
    return None


def cmd_chain_annotate(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    input_file = require_vcf_input(args)
    outdir = args.outdir or str(absolute_path(input_file).parent)
    annotations = [a.strip().lower() for a in args.annotations.split(",") if a.strip()]
    if not annotations:
        raise WGSExtractError("No valid annotations provided in --annotations.")
    unknown = [a for a in annotations if a != "vep" and a not in _VCF_STEPS]
    if unknown:
        raise WGSExtractError(
            f"Unknown annotation type(s): {', '.join(unknown)}. Valid: "
            f"{', '.join([*_VCF_STEPS, 'vep'])}"
        )

    logging.info(f"Starting chained annotation with: {', '.join(annotations)}")

    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)

    current_input = prepared_input = ensure_vcf_prepared(input_file)
    step_dirs: list[str] = []
    finalized = False

    try:
        for i, ann in enumerate(annotations):
            step = f"[{i + 1}/{len(annotations)}]"
            reason = skip_reason(ann, lib)
            if reason:
                logging.warning(f"{step} Skipping '{ann}': {reason}")
                continue

            step_outdir = str(Path(outdir, f"chain_step_{i + 1}_{ann}"))
            Path(step_outdir).mkdir(parents=True, exist_ok=True)
            step_dirs.append(step_outdir)
            logging.info(f"{step} Running '{ann}' annotation...")

            cmd = [
                sys.executable,
                "-m",
                "wgsextract_cli.main",
                *config_args(),
                *_step_command(ann),
            ]
            cmd += ["--input", current_input, "--outdir", step_outdir]
            if args.ref:
                cmd += ["--ref", args.ref]

            try:
                # Captured so chained runs stay readable; surfaced on failure.
                res = run_command(cmd, capture_output=True)
            except subprocess.CalledProcessError as e:
                for line in (e.stderr or "").strip().splitlines():
                    logging.error(f"  [{ann} ERROR] {line}")
                raise WGSExtractError(f"{step} Annotation step '{ann}' failed.") from e

            step_out = _step_output(ann, current_input, step_outdir)
            if not Path(step_out).exists():
                for line in (res.stderr or "").strip().splitlines():
                    logging.error(f"  [{ann} STDERR] {line}")
                raise WGSExtractError(f"{step} '{ann}' did not produce {step_out}.")

            current_input = step_out
            logging.info(f"{step} '{ann}' completed: {step_out}")

        if current_input == prepared_input:
            raise WGSExtractError(
                "No annotation was applied: every requested step was skipped."
            )
        final_out = str(Path(outdir, "chain_annotated.vcf.gz"))
        shutil.copy2(current_input, final_out)
        for ext in (".tbi", ".csi"):
            if Path(current_input + ext).exists():
                shutil.copy2(current_input + ext, final_out + ext)
        finalized = True
        logging.info(f"Chain annotation complete: {final_out}")
    finally:
        if not getattr(args, "keep_intermediates", False):
            if finalized:
                logging.info("Cleaning up intermediate files...")
                for step_outdir in step_dirs:
                    shutil.rmtree(step_outdir, ignore_errors=True)
            elif step_dirs:
                logging.warning(
                    "Preserving chain annotation intermediates because "
                    "finalization did not complete."
                )
