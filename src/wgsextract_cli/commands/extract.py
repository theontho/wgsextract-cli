import argparse
import contextlib
import logging
import subprocess
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_sam_index_cmd,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    get_chr_name,
)
from wgsextract_cli.core.warnings import print_warning

from ._extract_helpers import (
    BaseArgs,
    cleanup_files,
    derive_output_path,
    get_base_args,
    resolve_region_or_gene,
)

_SAMTOOLS = ["samtools"]
_CALLING_TOOLS = ["samtools", "bcftools", "tabix"]


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "extract", help="Extract specific chromosomes or unmapped reads."
    )
    ext_subs = parser.add_subparsers(dest="ext_cmd", required=True)

    # Mitochondrial commands
    mito_fasta_parser = ext_subs.add_parser(
        "mito-fasta", parents=[base_parser], help=CLI_HELP["cmd_mito-fasta"]
    )
    mito_fasta_parser.set_defaults(func=cmd_mito_fasta)

    mito_vcf_parser = ext_subs.add_parser(
        "mito-vcf", parents=[base_parser], help=CLI_HELP["cmd_mito-vcf"]
    )
    mito_vcf_parser.set_defaults(func=cmd_mito_vcf)

    mt_bam_parser = ext_subs.add_parser(
        "mt-bam", parents=[base_parser], help=CLI_HELP["cmd_mt-bam"]
    )
    mt_bam_parser.set_defaults(func=cmd_mt_bam)

    subset_parser = ext_subs.add_parser(
        "bam-subset", parents=[base_parser], help=CLI_HELP["cmd_bam-subset"]
    )
    subset_parser.add_argument(
        "--fraction",
        "-f",
        required=True,
        type=float,
        help=CLI_HELP["arg_fraction"],
    )
    subset_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    subset_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    subset_parser.set_defaults(func=cmd_bam_subset)

    # Y-DNA commands
    y_bam_parser = ext_subs.add_parser(
        "ydna-bam", parents=[base_parser], help=CLI_HELP["cmd_ydna-bam"]
    )
    y_bam_parser.set_defaults(func=cmd_ydna_bam)

    y_vcf_parser = ext_subs.add_parser(
        "ydna-vcf", parents=[base_parser], help=CLI_HELP["cmd_ydna-vcf"]
    )
    y_vcf_parser.set_defaults(func=cmd_ydna_vcf)

    # Combined command
    y_mt_parser = ext_subs.add_parser(
        "y-mt-extract", parents=[base_parser], help=CLI_HELP["cmd_y-mt-extract"]
    )
    y_mt_parser.set_defaults(func=cmd_y_mt_extract)

    # Legacy / Other
    unmapped_parser = ext_subs.add_parser(
        "unmapped", parents=[base_parser], help=CLI_HELP["cmd_unmapped"]
    )
    unmapped_parser.set_defaults(func=cmd_unmapped)

    custom_parser = ext_subs.add_parser(
        "custom", parents=[base_parser], help=CLI_HELP["cmd_custom"]
    )
    custom_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    custom_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    custom_parser.set_defaults(func=cmd_custom)


def _setup(args: argparse.Namespace, tools: list[str]) -> BaseArgs:
    verify_dependencies(tools)
    log_dependency_info(tools)
    return get_base_args(args)


def require_reference(resolved_ref: str | None, task: str) -> str:
    if not resolved_ref:
        raise WGSExtractError(LOG_MESSAGES["ref_required_for"].format(task=task))
    return resolved_ref


def _extract_regions(
    base: BaseArgs,
    input_path: str,
    out_bam: str,
    regions: list[str],
    bed: str | None = None,
) -> None:
    with cleanup_files(on_failure=[out_bam, out_bam + ".bai"]):
        run_command(
            ["samtools", "view", "-bh", *base.cram_opt, "-@", base.threads]
            + (["-L", bed] if bed else [])
            + ["-o", out_bam, input_path, *regions]
        )
        run_command(get_sam_index_cmd(out_bam, threads=base.threads))


def _call_haploid_variants(bam: str, ref: str, out_vcf: str) -> None:
    """Call variants on a haploid contig (chrM / chrY) into an indexed VCF."""
    for stale_index in (out_vcf + ".tbi", out_vcf + ".csi"):
        with contextlib.suppress(FileNotFoundError):
            Path(stale_index).unlink()
    run_pipeline(
        [
            ["bcftools", "mpileup", "-Ou", "-f", ref, bam],
            ["bcftools", "call", "--ploidy", "1", "-mv", "-Oz", "-o", out_vcf],
        ],
        description="Variant calling",
    )
    ensure_vcf_indexed(out_vcf)


def cmd_mito_fasta(args: argparse.Namespace) -> None:
    base = _setup(args, _CALLING_TOOLS)
    ref = require_reference(base.ref, "mitochondrial extraction")

    print_warning("ButtonMitoFASTA", threads=base.threads)

    chr_m = get_chr_name(args.input, "MT", base.cram_opt)
    out_bam = derive_output_path(args.input, base.outdir, "_MT_temp.bam")
    out_vcf = derive_output_path(args.input, base.outdir, "_MT_temp.vcf.gz")
    out_fasta = derive_output_path(args.input, base.outdir, "_MT.fasta")

    temp = [out_bam, out_bam + ".bai", out_vcf, out_vcf + ".tbi"]
    try:
        with cleanup_files(temp=temp, on_failure=[out_fasta]):
            _extract_regions(base, args.input, out_bam, [chr_m])
            _call_haploid_variants(out_bam, ref, out_vcf)

            logging.info(LOG_MESSAGES["generating_consensus"].format(output=out_fasta))
            with open(out_fasta, "wb") as f:
                run_pipeline(
                    [
                        ["samtools", "faidx", ref, chr_m],
                        ["bcftools", "consensus", "-f", "-", "-H", "1", out_vcf],
                    ],
                    stdout=f,
                    description="Consensus generation",
                )
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Mito FASTA extraction failed: {e}") from e


def cmd_mito_vcf(args: argparse.Namespace) -> None:
    base = _setup(args, _CALLING_TOOLS)
    ref = require_reference(base.ref, "mitochondrial extraction")

    print_warning("ButtonMitoVCF", threads=base.threads)

    chr_m = get_chr_name(args.input, "MT", base.cram_opt)
    out_bam = derive_output_path(args.input, base.outdir, "_MT_temp.bam")
    out_vcf = derive_output_path(args.input, base.outdir, "_MT.vcf.gz")

    try:
        with cleanup_files(temp=[out_bam, out_bam + ".bai"], on_failure=[out_vcf]):
            _extract_regions(base, args.input, out_bam, [chr_m])
            logging.info(LOG_MESSAGES["calling_mito_variants"].format(output=out_vcf))
            _call_haploid_variants(out_bam, ref, out_vcf)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Mito VCF extraction failed: {e}") from e


def cmd_mt_bam(args: argparse.Namespace) -> None:
    base = _setup(args, _SAMTOOLS)

    mt_chr = get_chr_name(args.input, "MT", base.cram_opt)
    out_file = derive_output_path(args.input, base.outdir, "_mtDNA.bam")

    logging.info(LOG_MESSAGES["extracting_mt"].format(mt_chr=mt_chr, output=out_file))
    try:
        _extract_regions(base, args.input, out_file, [mt_chr])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"mtDNA extraction failed: {e}") from e


def cmd_bam_subset(args: argparse.Namespace) -> None:
    base = _setup(args, _SAMTOOLS)
    region = resolve_region_or_gene(args, base.ref)

    out_file = derive_output_path(args.input, base.outdir, "_subset.bam")
    logging.info(
        LOG_MESSAGES["subsetting_file"].format(fraction=args.fraction, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            run_command(
                ["samtools", "view", "-bh", "-s", str(args.fraction), *base.cram_opt]
                + ["-o", out_file, args.input, *([region] if region else [])]
            )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"BAM subsetting failed: {e}") from e


def cmd_ydna_bam(args: argparse.Namespace) -> None:
    base = _setup(args, _SAMTOOLS)

    print_warning("ButtonYOnlyBAM", threads=base.threads)

    chr_y = get_chr_name(args.input, "Y", base.cram_opt)
    out_bam = derive_output_path(args.input, base.outdir, "_Y.bam")

    logging.info(LOG_MESSAGES["extracting_y"].format(chr_y=chr_y, output=out_bam))
    try:
        _extract_regions(base, args.input, out_bam, [chr_y])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Y BAM extraction failed: {e}") from e


def cmd_ydna_vcf(args: argparse.Namespace) -> None:
    base = _setup(args, _CALLING_TOOLS)
    ref = require_reference(base.ref, "Y extraction")

    print_warning("ButtonYOnlyVCF", threads=base.threads)

    chr_y = get_chr_name(args.input, "Y", base.cram_opt)
    out_bam = derive_output_path(args.input, base.outdir, "_Y_temp.bam")
    out_vcf = derive_output_path(args.input, base.outdir, "_Y.vcf.gz")

    try:
        with cleanup_files(temp=[out_bam, out_bam + ".bai"], on_failure=[out_vcf]):
            _extract_regions(base, args.input, out_bam, [chr_y])
            logging.info(LOG_MESSAGES["calling_y_variants"].format(output=out_vcf))
            _call_haploid_variants(out_bam, ref, out_vcf)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Y VCF extraction failed: {e}") from e


def cmd_y_mt_extract(args: argparse.Namespace) -> None:
    base = _setup(args, _SAMTOOLS)

    print_warning("ButtonYMTBAM", threads=base.threads)

    chr_y = get_chr_name(args.input, "Y", base.cram_opt)
    chr_m = get_chr_name(args.input, "MT", base.cram_opt)
    out_bam = derive_output_path(args.input, base.outdir, "_Y_MT.bam")

    logging.info(LOG_MESSAGES["extracting_y_mt"].format(output=out_bam))
    try:
        _extract_regions(base, args.input, out_bam, [chr_y, chr_m])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Y+MT extraction failed: {e}") from e


def cmd_unmapped(args: argparse.Namespace) -> None:
    base = _setup(args, _SAMTOOLS)

    print_warning("ButtonUnmappedBAM", threads=base.threads)

    out_bam = derive_output_path(args.input, base.outdir, "_unmapped.bam")

    logging.info(LOG_MESSAGES["extracting_unmapped"].format(output=out_bam))
    try:
        with cleanup_files(on_failure=[out_bam]):
            # -f 4 gets unmapped reads
            run_command(
                [
                    "samtools",
                    "view",
                    "-bh",
                    "-f",
                    "4",
                    *base.cram_opt,
                    "-@",
                    base.threads,
                ]
                + ["-o", out_bam, args.input]
            )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Unmapped extraction failed: {e}") from e


def cmd_custom(args: argparse.Namespace) -> None:
    base = _setup(args, _SAMTOOLS)
    region = resolve_region_or_gene(args, base.ref)
    if not region:
        raise WGSExtractError("--region or --gene is required for custom extraction.")

    is_bed = Path(region).is_file()
    label = Path(region).name if is_bed else region
    region_suffix = label.replace(":", "_").replace(",", "_")
    out_bam = derive_output_path(args.input, base.outdir, f"_{region_suffix}.bam")

    logging.info(
        LOG_MESSAGES["extracting_region"].format(region=region, output=out_bam)
    )
    try:
        if is_bed:
            _extract_regions(base, args.input, out_bam, [], bed=region)
        else:
            # Split comma-separated regions (e.g. chr1:1-100,chr2:200-300)
            _extract_regions(base, args.input, out_bam, region.split(","))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Custom extraction failed: {e}") from e
