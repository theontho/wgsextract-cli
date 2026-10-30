import argparse
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from wgsextract_cli.core.alignment_health import warn_about_alignment
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
from wgsextract_cli.core.microarray_aadr import (
    aadr_lifter,
    aadr_panel,
    aadr_vcf_nocalls,
    build_aadr_target,
    validate_aadr_sample_id,
    write_aadr_outputs,
)
from wgsextract_cli.core.microarray_quality import quality_policy
from wgsextract_cli.core.microarray_targets import (
    build_genome_wide_target,
    is_genome_wide_target,
    ncbi36_chain,
)
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import input_stem, resolve_reference_library
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.sample_ploidy import sex_samples_args
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
    _alignment_chromosomes,
    _normalize_region_args_for_input,
    _prepare_microarray_vcf,
)

_MICROARRAY_TEMPLATE_MAP = {
    # 23andMe's v1 and v2 chips export the same marker list.
    "23andme_v1": "23andMe_V2",
    "23andme_v2": "23andMe_V2",
    "23andme_v3": "23andMe_V3",
    "23andme_v4": "23andMe_V4",
    "23andme_v5": "23andMe_V5",
    "23andme_v5_2025": "23andMe_V5_2025",
    "23andme_v3+v5": "23andMe_V35",
    "23andme_v3_v5": "23andMe_V35",
    "23andme_v35": "23andMe_V35",
    "23andme_api": "23andMe_SNPs_API",
    "ancestry_v1": "Ancestry_V1",
    "ancestry_v2": "Ancestry_V2",
    "ancestry_v2_2021": "Ancestry_V2_2021",
    "ancestry_v2_2024": "Ancestry_V2_2024",
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
    "myheritage_gsa_2025": "MyHeritage_GSA_2025",
    "myheritage_gsa_2025_609424": "MyHeritage_GSA_2025_609424",
    "nucleus": "Nucleus_V1",
    "nucleus_v1": "Nucleus_V1",
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


def _ncbi36_chain(reflib_dir: str) -> str | None:
    """The hg18ToHg19 chain, or None to match NCBI36 markers by rsID."""
    try:
        return ncbi36_chain(reflib_dir)
    except WGSExtractError as e:
        logging.warning(str(e))
        return None


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
    aadr_vcf: str | None = None,
    aadr_workdir: str | None = None,
) -> None:
    from wgsextract_cli.core.microarray_utils import (
        convert_to_vendor_format,
        get_template_format,
        liftover_hg38_to_hg19,
    )

    template_search_roots = _template_search_roots(ref_fasta, lib)
    requested_formats = args.formats.split(",")

    # 3. Liftover if needed (vendor templates use hg19 coordinates)
    final_txt = combined_kit_txt
    chain = (
        _liftover_chain_for(lib)
        if any(aadr_panel(key) is None for key in requested_formats)
        else None
    )
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
    start_fmt = time.time()
    aadr_liftover = None
    vcf_nocalls = None
    written_panels: set[str] = set()
    for fmt_key in requested_formats:
        panel = aadr_panel(fmt_key)
        if panel:
            if panel in written_panels:
                continue
            output_prefix = str(Path(outdir, f"{base_name}_AADR_{panel}"))
            try:
                if not written_panels:
                    aadr_liftover = aadr_lifter(
                        lib.build, aadr_workdir or outdir, getattr(lib, "root", None)
                    )
                    if aadr_vcf:
                        vcf_nocalls = aadr_vcf_nocalls(aadr_vcf, aadr_workdir or outdir)
                write_aadr_outputs(
                    panel,
                    combined_kit_txt,
                    output_prefix,
                    sample_id=getattr(args, "aadr_sample_id", "sample"),
                    sex=getattr(args, "aadr_sex", "U"),
                    lifter=aadr_liftover,
                    vcf_nocalls=vcf_nocalls,
                )
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                WGSExtractError,
            ) as e:
                raise WGSExtractError(f"Failed to generate AADR {panel}: {e}") from e
            written_panels.add(panel)
            continue
        real_fmt = _resolve_microarray_format(fmt_key)
        if real_fmt is None:
            continue

        logging.info(LOG_MESSAGES["micro_generating_fmt"].format(format=real_fmt))

        fmt_info = get_template_format(real_fmt)
        suffix = fmt_info["suffix"] if fmt_info else ".txt"
        output_file = str(Path(outdir, f"{base_name}_{real_fmt}{suffix}"))
        try:
            chain36 = (
                _ncbi36_chain(lib.root)
                if fmt_info and fmt_info.get("ncbi36") and lib.root
                else None
            )
            convert_to_vendor_format(
                real_fmt, final_txt, output_file, template_search_roots, chain36
            )
        except (OSError, ValueError, WGSExtractError) as e:
            raise WGSExtractError(f"Failed to generate {real_fmt}: {e}") from e
        logging.info(f"Generated {output_file}")
    fmt_duration = time.time() - start_fmt
    logging.info(f"Format conversion (all) took {fmt_duration:.2f}s")

    total_duration = time.time() - start_total
    logging.info(f"Total microarray process took {total_duration:.2f}s")


def run(args: argparse.Namespace) -> None:
    quality_policy(args)
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})

    verify_dependencies(["bcftools", "tabix", "bgzip", "samtools"])
    log_dependency_info(["bcftools", "tabix", "bgzip", "samtools"])

    logging.debug(f"Input file: {str(absolute_path(args.input))}")

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else str(absolute_path(args.input).parent)
    calling_outdir = outdir
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    md5_sig = calculate_bam_md5(args.input, None)
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=args.input)

    ref_fasta = lib.fasta
    ref_vcf_tab = args.ref_vcf_tab if args.ref_vcf_tab else lib.ref_vcf_tab
    ploidy_file = args.ploidy_file if args.ploidy_file else lib.ploidy_file
    requested = args.formats.split(",")
    aadr_panels = [panel for key in requested if (panel := aadr_panel(key))]
    legacy_requested = any(aadr_panel(key) is None for key in requested)
    if aadr_panels:
        validate_aadr_sample_id(getattr(args, "aadr_sample_id", "sample"))

    logging.debug(f"Resolved Reference FASTA: {ref_fasta}")
    logging.debug(f"Resolved Target SNP Tab: {ref_vcf_tab}")
    logging.debug(f"Resolved Ploidy File: {ploidy_file}")
    if lib.liftover_chain:
        logging.debug(f"Resolved Liftover Chain: {lib.liftover_chain}")

    if not ref_fasta or not Path(ref_fasta).is_file():
        message = LOG_MESSAGES["ref_required_for"].format(task="microarray generation")
        raise WGSExtractError(message)

    if (
        not args.ref_vcf_tab
        and (not aadr_panels or legacy_requested)
        and lib.root
        and lib.build
        and (not ref_vcf_tab or is_genome_wide_target(ref_vcf_tab, lib.root, lib.build))
    ):
        # A library set up with `ref bootstrap` has the chip templates but no
        # genome-wide target: build it from them, and rebuild one an older
        # version of the CLI built.
        ref_vcf_tab = build_genome_wide_target(
            lib.root, lib.build, _template_search_roots(ref_fasta, lib)
        )

    if aadr_panels:
        Path(outdir).mkdir(parents=True, exist_ok=True)
        calling_outdir = tempfile.mkdtemp(prefix="AADR_", dir=outdir)
        try:
            ref_vcf_tab = build_aadr_target(
                aadr_panels,
                calling_outdir,
                aadr_lifter(lib.build, calling_outdir, lib.root),
                ref_vcf_tab if args.ref_vcf_tab or legacy_requested else None,
            )
        except (OSError, ValueError) as e:
            raise WGSExtractError(f"Could not prepare AADR targets: {e}") from e

    if not ref_vcf_tab:
        message = "--ref-vcf-tab is required and could not be auto-resolved."
        raise WGSExtractError(message)

    if not Path(ref_vcf_tab).is_file():
        message = f"--ref-vcf-tab not found: {ref_vcf_tab}"
        raise WGSExtractError(message)

    if not aadr_panels or legacy_requested:
        _liftover_chain_for(lib)  # fail before variant calling, not after
    Path(outdir).mkdir(parents=True, exist_ok=True)

    start_total = time.time()

    print_warning("ButtonMicroarray", threads=threads)

    # 1. Variant Calling or VCF Extraction
    base_name = input_stem(args.input)
    out_vcf = str(Path(calling_outdir, f"{base_name}_combined.vcf.gz"))
    is_vcf = args.input.endswith((".vcf", ".vcf.gz", ".bcf"))
    if not is_vcf:
        warn_about_alignment(args.input, ref_fasta)

    region_args = ["-r", args.region] if args.region else []
    input_chroms = None
    if args.region and not is_vcf:
        # Custom ploidy rows must be matched against the same chromosome name
        # used for calling, including a chr20 <-> 20 input alias.
        try:
            input_chroms = _alignment_chromosomes(args.input, ref_fasta)
            region_args = _normalize_region_args_for_input(region_args, input_chroms)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            raise WGSExtractError(f"Microarray target normalization failed: {e}") from e

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

    with sex_samples_args(
        args.input,
        outdir,
        ploidy_args,
        region=region_args[1] if region_args else None,
    ) as sex_args:
        out_vcf, ref_vcf_tab = _prepare_microarray_vcf(
            args=args,
            outdir=calling_outdir,
            base_name=base_name,
            is_vcf=is_vcf,
            ref_vcf_tab=ref_vcf_tab,
            region_args=region_args,
            ploidy_args=ploidy_args + sex_args,
            ref_fasta=ref_fasta,
            threads=threads,
            start_vcf=start_vcf,
            out_vcf=out_vcf,
            input_chroms=input_chroms,
        )
    combined_kit_txt = _write_microarray_combined_kit(
        args=args,
        outdir=calling_outdir,
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
        aadr_vcf=out_vcf if is_vcf and aadr_panels else None,
        aadr_workdir=calling_outdir if aadr_panels else None,
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
        "--min-gq",
        type=int,
        default=None,
        help="Minimum sample GQ; missing GQ becomes a no-call (default: config or 0, disabled)",
    )
    parser.add_argument(
        "--min-dp",
        type=int,
        default=None,
        help="Minimum sample DP; missing DP becomes a no-call (default: config or 0, disabled)",
    )
    parser.add_argument(
        "--assume-hom-ref",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Assume absent VCF markers are reference on represented contigs (default: config or true)",
    )
    parser.add_argument(
        "--exclude-filtered",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="No-call VCF records failing FILTER (default: config or false)",
    )
    parser.add_argument(
        "--call-reasons",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Also write CombinedKit_reasons.tsv with explicit reasons (default: config or false)",
    )
    parser.add_argument(
        "--aadr-sample-id",
        default="sample",
        help="Non-identifying sample label for AADR .fam/.ind (default: sample)",
    )
    parser.add_argument(
        "--aadr-sex",
        choices=("U", "M", "F"),
        default="U",
        help="AADR metadata sex only; does not change genotype calling (default: U)",
    )
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
