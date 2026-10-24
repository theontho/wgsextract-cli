import argparse
import logging
import time
from pathlib import Path

from wgsextract_cli.core.alt_mapping import warn_if_alt_unaware
from wgsextract_cli.core.builds import (
    is_hg37_build,
    is_hg38_build,
    is_t2t_build,
    ploidy_for_build,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.microarray_targets import build_genome_wide_target
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import input_stem, resolve_reference_library
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
    # 23andMe's v1 and v2 chips export the same marker list.
    "23andme_v1": "23andMe_V2",
    "23andme_v2": "23andMe_V2",
    "23andme_v3": "23andMe_V3",
    "23andme_v4": "23andMe_V4",
    "23andme_v5": "23andMe_V5",
    "23andme_v3+v5": "23andMe_V35",
    "23andme_v3_v5": "23andMe_V35",
    "23andme_v35": "23andMe_V35",
    "23andme_api": "23andMe_SNPs_API",
    "ancestry_v1": "Ancestry_V1",
    "ancestry_v2": "Ancestry_V2",
    "ftdna_v1": "FTDNA_V1_Affy",
    "ftdna_v1_affy": "FTDNA_V1_Affy",
    "familytreedna_v1": "FTDNA_V1_Affy",
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


_PACKAGED_ASSETS = absolute_path(__file__).parents[1] / "assets"


def _resolve_microarray_format(format_key: str) -> str | None:
    normalized = format_key.strip().lower().replace("-", "_")
    if normalized in {"", "all", "combined_all"}:
        return None
    return _MICROARRAY_TEMPLATE_MAP.get(normalized, normalized)


def _template_search_roots(ref_fasta: str, lib: ReferenceLibrary) -> list[str]:
    roots = []
    ref_dir = str(Path(ref_fasta).parent)
    if ref_dir:
        roots.append(ref_dir)
    # Templates ship with the reference library (`ref bootstrap`); search it too
    # when --ref is a FASTA kept elsewhere. Last, the few the CLI ships itself.
    for root in (lib.root, resolve_reference_library(), str(_PACKAGED_ASSETS)):
        if root and root not in roots:
            roots.append(root)
    return roots


def _liftover_chain_for(lib: ReferenceLibrary) -> str | None:
    """Return the hg38->hg19 chain when liftover is needed (vendor templates use hg19)."""
    if lib.build and is_t2t_build(lib.build):
        raise WGSExtractError(
            "Microarray formats use hg19 coordinates; T2T input is not supported."
        )
    if not (lib.build and is_hg38_build(lib.build)):
        return None
    if not lib.liftover_chain:
        raise WGSExtractError(
            "hg38 input needs the hg38ToHg19 liftover chain for microarray "
            "formats, but none was found in the reference library."
        )
    return lib.liftover_chain


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
        get_template_format,
        liftover_hg38_to_hg19,
    )

    template_search_roots = _template_search_roots(ref_fasta, lib)

    # 3. Liftover if needed (vendor templates use hg19 coordinates)
    final_txt = combined_kit_txt
    chain = _liftover_chain_for(lib)
    if chain:
        combined_path = Path(combined_kit_txt)
        hg19_txt = str(combined_path.with_name(combined_path.stem + "_hg19.txt"))
        logging.info(LOG_MESSAGES["micro_liftover_warn"])
        start_lift = time.time()
        try:
            liftover_hg38_to_hg19(
                combined_kit_txt,
                hg19_txt,
                chain,
                templates_dir=template_search_roots,
            )
        except (OSError, ValueError) as e:
            raise WGSExtractError(f"Microarray liftover failed: {e}") from e
        final_txt = hg19_txt
        logging.info(f"Liftover took {time.time() - start_lift:.2f}s")

    # 4. Convert to vendor formats
    requested_formats = args.formats.split(",")
    start_fmt = time.time()
    for fmt_key in requested_formats:
        real_fmt = _resolve_microarray_format(fmt_key)
        if real_fmt is None:
            continue

        logging.info(LOG_MESSAGES["micro_generating_fmt"].format(format=real_fmt))

        fmt_info = get_template_format(real_fmt)
        suffix = fmt_info["suffix"] if fmt_info else ".txt"
        output_file = str(Path(outdir, f"{base_name}_{real_fmt}{suffix}"))
        try:
            convert_to_vendor_format(
                real_fmt, final_txt, output_file, template_search_roots
            )
        except (OSError, ValueError, WGSExtractError) as e:
            raise WGSExtractError(f"Failed to generate {real_fmt}: {e}") from e
        logging.info(f"Generated {output_file}")
    fmt_duration = time.time() - start_fmt
    logging.info(f"Format conversion (all) took {fmt_duration:.2f}s")

    total_duration = time.time() - start_total
    logging.info(f"Total microarray process took {total_duration:.2f}s")


def run(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})

    verify_dependencies(["bcftools", "tabix", "bgzip", "samtools"])
    log_dependency_info(["bcftools", "tabix", "bgzip", "samtools"])

    logging.debug(f"Input file: {str(absolute_path(args.input))}")

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else str(absolute_path(args.input).parent)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

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

    if not ref_fasta or not Path(ref_fasta).is_file():
        message = LOG_MESSAGES["ref_required_for"].format(task="microarray generation")
        raise WGSExtractError(message)

    if not ref_vcf_tab and lib.root and lib.build:
        # A library set up with `ref bootstrap` has the chip templates but no
        # genome-wide target: build it from them (once).
        ref_vcf_tab = build_genome_wide_target(
            lib.root, lib.build, _template_search_roots(ref_fasta, lib)
        )

    if not ref_vcf_tab:
        message = "--ref-vcf-tab is required and could not be auto-resolved."
        raise WGSExtractError(message)

    if not Path(ref_vcf_tab).is_file():
        message = f"--ref-vcf-tab not found: {ref_vcf_tab}"
        raise WGSExtractError(message)

    _liftover_chain_for(lib)  # fail before variant calling, not after
    Path(outdir).mkdir(parents=True, exist_ok=True)

    start_total = time.time()

    print_warning("ButtonMicroarray", threads=threads)

    # 1. Variant Calling or VCF Extraction
    base_name = input_stem(args.input)
    out_vcf = str(Path(outdir, f"{base_name}_combined.vcf.gz"))
    is_vcf = args.input.endswith((".vcf", ".vcf.gz", ".bcf"))
    if not is_vcf:
        warn_if_alt_unaware(args.input, ref_fasta)

    region_args = ["-r", args.region] if args.region else []

    # Without a ploidy file, use the build's predefined ploidy; for an unknown
    # build fall back to all-diploid (never haploid autosomes).
    if ploidy_file:
        ploidy_args = ["--ploidy-file", ploidy_file]
    elif lib.build and (is_hg37_build(lib.build) or is_hg38_build(lib.build)):
        ploidy_args = ["--ploidy", ploidy_for_build(lib.build)]
    else:
        logging.warning(
            f"Unknown reference build {lib.build!r}; calling all chromosomes diploid."
        )
        ploidy_args = ["--ploidy", "2"]

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
