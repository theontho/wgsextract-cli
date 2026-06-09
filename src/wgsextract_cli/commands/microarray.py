import argparse
import logging
import os
import time

from wgsextract_cli.core.builds import (
    is_hg38_build,
    ploidy_for_build,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import print_warning

from ._microarray_combined import (
    _write_microarray_combined_kit,
)
from ._microarray_vcf import (
    _prepare_microarray_vcf,
)

_MICROARRAY_TEMPLATE_MAP = {
    "23andme_v3": "23andMe_V3",
    "23andme_v4": "23andMe_V4",
    "23andme_v5": "23andMe_V5",
    "23andme_v3+v5": "23andMe_V35",
    "23andme_v3_v5": "23andMe_V35",
    "23andme_v35": "23andMe_V35",
    "23andme_api": "23andMe_SNPs_API",
    "ancestry_v1": "Ancestry_V1",
    "ancestry_v2": "Ancestry_V2",
    "ftdna_v2": "FTDNA_V2",
    "ftdna_v3": "FTDNA_V3",
    "familytreedna_v2": "FTDNA_V2",
    "familytreedna_v3": "FTDNA_V3",
    "ldna_v1": "LDNA_V1",
    "ldna_v2": "LDNA_V2",
    "livingdna_v1": "LDNA_V1",
    "livingdna_v2": "LDNA_V2",
    "myheritage_v1": "MyHeritage_V1",
    "myheritage_v2": "MyHeritage_V2",
}


def _resolve_microarray_format(format_key: str) -> str | None:
    normalized = format_key.strip().lower().replace("-", "_")
    if normalized in {"", "all", "combined_all"}:
        return None
    return _MICROARRAY_TEMPLATE_MAP.get(normalized, normalized)


def _template_search_roots(ref_fasta: str, lib: ReferenceLibrary) -> list[str]:
    roots = []
    ref_dir = os.path.dirname(ref_fasta)
    if ref_dir:
        roots.append(ref_dir)
    if lib.root and lib.root not in roots:
        roots.append(lib.root)
    return roots


def _convert_microarray_outputs(
    *,
    args: argparse.Namespace,
    outdir: str,
    base_name: str,
    lib: ReferenceLibrary,
    combined_kit_txt: str,
    ref_fasta: str,
    start_total: float,
) -> None:
    from wgsextract_cli.core.microarray_utils import (
        convert_to_vendor_format,
        liftover_hg38_to_hg19,
    )

    template_search_roots = _template_search_roots(ref_fasta, lib)

    # 3. Liftover if needed (to hg19 for most vendors)
    final_txt = combined_kit_txt
    if lib.build and is_hg38_build(lib.build):
        hg19_txt = combined_kit_txt.replace(".txt", "_hg19.txt")
        if lib.liftover_chain:
            logging.info(LOG_MESSAGES["micro_liftover_warn"])
            start_lift = time.time()
            try:
                liftover_hg38_to_hg19(
                    combined_kit_txt,
                    hg19_txt,
                    lib.liftover_chain,
                    templates_dir=template_search_roots,
                )
                final_txt = hg19_txt
                lift_duration = time.time() - start_lift
                logging.info(f"Liftover took {lift_duration:.2f}s")
            except (OSError, ValueError, WGSExtractError) as e:
                logging.error(f"Liftover failed: {e}")
                raise WGSExtractError("Microarray liftover failed.") from e
        else:
            logging.warning("Liftover requested but chain file not found.")

    # 4. Convert to vendor formats
    requested_formats = args.formats.split(",")
    start_fmt = time.time()
    for fmt_key in requested_formats:
        real_fmt = _resolve_microarray_format(fmt_key)
        if real_fmt is None:
            continue

        logging.info(LOG_MESSAGES["micro_generating_fmt"].format(format=real_fmt))

        output_file = os.path.join(outdir, f"{base_name}_{real_fmt}.txt")
        if "MyHeritage" in real_fmt or "FTDNA" in real_fmt:
            output_file = output_file.replace(".txt", ".csv")

        try:
            convert_to_vendor_format(
                real_fmt, final_txt, output_file, template_search_roots
            )
            logging.info(f"Generated {output_file}")
        except WGSExtractError as e:
            logging.error(f"Failed to generate {real_fmt}: {e}")
            raise
        except (OSError, ValueError) as e:
            logging.error(f"Failed to generate {real_fmt}: {e}")
            raise WGSExtractError(f"Failed to generate {real_fmt}: {e}") from e
    fmt_duration = time.time() - start_fmt
    logging.info(f"Format conversion (all) took {fmt_duration:.2f}s")

    total_duration = time.time() - start_total
    logging.info(f"Total microarray process took {total_duration:.2f}s")


def run(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix", "samtools"])
    log_dependency_info(["bcftools", "tabix", "samtools"])

    if not args.input:
        logging.error(LOG_MESSAGES["input_required"])
        return

    if not verify_paths_exist({"--input": args.input}):
        return

    logging.debug(f"Input file: {os.path.abspath(args.input)}")

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.input))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    md5_sig = calculate_bam_md5(args.input, None)
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=args.input)

    ref_fasta = lib.fasta
    ref_vcf_tab = args.ref_vcf_tab if args.ref_vcf_tab else lib.ref_vcf_tab
    ploidy_file = args.ploidy_file if args.ploidy_file else lib.ploidy_file

    logging.debug(f"Resolved Reference FASTA: {ref_fasta}")
    logging.debug(f"Resolved Target SNP Tab: {ref_vcf_tab}")
    logging.debug(f"Resolved Ploidy File: {ploidy_file}")
    if lib.liftover_chain:
        logging.debug(f"Resolved Liftover Chain: {lib.liftover_chain}")

    if not ref_fasta or not os.path.isfile(ref_fasta):
        message = LOG_MESSAGES["ref_required_for"].format(task="microarray generation")
        raise WGSExtractError(message)

    if not ref_vcf_tab:
        message = "--ref-vcf-tab is required and could not be auto-resolved."
        raise WGSExtractError(message)

    if not os.path.isfile(ref_vcf_tab):
        message = f"--ref-vcf-tab not found: {ref_vcf_tab}"
        raise WGSExtractError(message)

    start_total = time.time()

    print_warning("ButtonMicroarray", threads=threads)

    # 1. Variant Calling or VCF Extraction
    base_name = os.path.basename(args.input).split(".")[0]
    out_vcf = os.path.join(outdir, f"{base_name}_combined.vcf.gz")
    is_vcf = args.input.endswith((".vcf", ".vcf.gz", ".bcf"))

    region_args = ["-r", args.region] if args.region else []

    # Resolve ploidy alias from build if no file provided
    ploidy_val = "1"  # Default to haploid if unknown
    if lib.build:
        try:
            ploidy_val = ploidy_for_build(lib.build)
        except ValueError:
            pass

    ploidy_args = (
        ["--ploidy-file", ploidy_file] if ploidy_file else ["--ploidy", ploidy_val]
    )

    start_vcf = time.time()

    out_vcf, ref_vcf_tab = _prepare_microarray_vcf(
        args=args,
        outdir=outdir,
        base_name=base_name,
        is_vcf=is_vcf,
        ref_vcf_tab=ref_vcf_tab,
        region_args=region_args,
        ploidy_args=ploidy_args,
        ref_fasta=ref_fasta,
        threads=threads,
        start_vcf=start_vcf,
        out_vcf=out_vcf,
    )
    combined_kit_txt = _write_microarray_combined_kit(
        args=args,
        outdir=outdir,
        base_name=base_name,
        is_vcf=is_vcf,
        out_vcf=out_vcf,
        ref_fasta=ref_fasta,
        ref_vcf_tab=ref_vcf_tab,
    )
    _convert_microarray_outputs(
        args=args,
        outdir=outdir,
        base_name=base_name,
        lib=lib,
        combined_kit_txt=combined_kit_txt,
        ref_fasta=ref_fasta,
        start_total=start_total,
    )


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "microarray",
        parents=[base_parser],
        help=CLI_HELP["cmd_microarray"],
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--formats", default="all", help=CLI_HELP["micro_formats_help"])
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Enable per-chromosome parallel variant calling",
    )
    parser.add_argument(
        "--ref-vcf-tab",
        help="Master tabulated list of all consumer microarray SNPs (auto-resolved from --ref if possible)",
    )
    parser.add_argument(
        "--ploidy-file",
        help="File defining ploidy per chromosome (auto-resolved from --ref if possible)",
    )
    parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    parser.set_defaults(func=run)
