import logging
import os

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import get_resource_defaults
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    resolve_reference,
    verify_paths_exist,
)


def resolve_region_or_gene(args, resolved_ref):
    """Helper to resolve either a raw region or a gene name to coordinates."""
    if hasattr(args, "region") and args.region:
        return args.region

    if hasattr(args, "gene") and args.gene:
        from wgsextract_cli.core.config import settings

        build = "hg38"
        if resolved_ref and (
            "hg19" in resolved_ref.lower() or "b37" in resolved_ref.lower()
        ):
            build = "hg19"

        from wgsextract_cli.core.gene_map import GeneMap, resolve_gene_map_reflib

        reflib_dir = resolve_gene_map_reflib(
            resolved_ref, settings.get("reference_library"), build
        )
        if not reflib_dir:
            logging.error(
                "Reference library not found. Please provide a --ref or set reference_library in config.toml."
            )
            return None

        gm = GeneMap(reflib_dir)
        resolved_region = gm.get_coords(args.gene, build)
        if resolved_region:
            logging.info(f"Resolved gene {args.gene} to {resolved_region}")
            return resolved_region
        logging.error(f"Could not resolve gene name: {args.gene}")
        return None

    return None


def get_base_args(args):
    verify_dependencies(["samtools", "bcftools", "tabix"])
    log_dependency_info(["samtools", "bcftools", "tabix"])

    if not args.input:
        logging.error(LOG_MESSAGES["input_required"])
        return None

    if not verify_paths_exist({"--input": args.input}):
        return None

    logging.debug(f"Input file: {os.path.abspath(args.input)}")

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.input))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    md5_sig = calculate_bam_md5(args.input, None)
    resolved_ref = resolve_reference(args.ref, md5_sig)
    logging.debug(f"Resolved reference: {resolved_ref}")

    paths_to_check = {}
    if resolved_ref:
        paths_to_check["--ref"] = resolved_ref

    if not verify_paths_exist(paths_to_check):
        return None

    cram_opt = ["-T", resolved_ref] if resolved_ref else []
    return threads, outdir, cram_opt, resolved_ref
