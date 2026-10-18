import argparse
import logging
import os
import subprocess
import tempfile

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_sam_index_cmd,
    get_sam_sort_cmd,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import (
    check_free_space,
    get_free_space_needed,
    print_warning,
)

from ._extract_helpers import (
    alignments_to_fastq,
    cleanup_files,
    derive_output_path,
    get_base_args,
    resolve_region_or_gene,
)


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("bam", help=CLI_HELP["cmd_bam_mgmt"])
    bam_subs = parser.add_subparsers(dest="bam_cmd", required=True)

    sort_parser = bam_subs.add_parser(
        "sort", parents=[base_parser], help=CLI_HELP["cmd_sort"]
    )
    sort_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    sort_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    sort_parser.set_defaults(func=cmd_sort)

    index_parser = bam_subs.add_parser(
        "index", parents=[base_parser], help=CLI_HELP["cmd_index"]
    )
    index_parser.set_defaults(func=cmd_index)

    unindex_parser = bam_subs.add_parser(
        "unindex", parents=[base_parser], help=CLI_HELP["cmd_unindex"]
    )
    unindex_parser.set_defaults(func=cmd_unindex)

    unsort_parser = bam_subs.add_parser(
        "unsort", parents=[base_parser], help=CLI_HELP["cmd_unsort"]
    )
    unsort_parser.set_defaults(func=cmd_unsort)

    tocram_parser = bam_subs.add_parser(
        "to-cram", parents=[base_parser], help=CLI_HELP["cmd_to-cram"]
    )
    tocram_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    tocram_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    tocram_parser.add_argument(
        "--cram-version",
        choices=["2.1", "3.0", "3.1"],
        default="3.0",
        help="CRAM version to output (default: 3.0 for better compatibility)",
    )
    tocram_parser.set_defaults(func=cmd_tocram)

    tobam_parser = bam_subs.add_parser(
        "to-bam", parents=[base_parser], help=CLI_HELP["cmd_to-bam"]
    )
    tobam_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    tobam_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    tobam_parser.set_defaults(func=cmd_tobam)

    unalign_parser = bam_subs.add_parser(
        "unalign", parents=[base_parser], help=CLI_HELP["cmd_unalign"]
    )
    unalign_parser.add_argument("--r1", required=True, help=CLI_HELP["arg_r1"])
    unalign_parser.add_argument("--r2", required=True, help=CLI_HELP["arg_r2"])
    unalign_parser.add_argument("--se", help=CLI_HELP["arg_se"])
    unalign_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    unalign_parser.add_argument(
        "--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)"
    )
    unalign_parser.set_defaults(func=cmd_unalign)

    identify_parser = bam_subs.add_parser(
        "identify", parents=[base_parser], help=CLI_HELP["cmd_bam-identify"]
    )
    identify_parser.set_defaults(func=cmd_identify)


def _require_input(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})


def _alignment_format(path: str) -> tuple[str, str]:
    """Return (samtools format name, file extension) matching an input file."""
    return ("CRAM", ".cram") if path.lower().endswith(".cram") else ("BAM", ".bam")


def _preflight_sort_space(
    input_path: str, outdir: str, sort_type: str, app_name: str, region: str | None
) -> None:
    """Refuse a whole-file sort that cannot fit in outdir (regions are smaller)."""
    file_size = os.path.getsize(input_path)
    is_cram = input_path.lower().endswith(".cram")
    print_warning(
        "infoFreeSpace", app_name=app_name, file_size=file_size, is_cram=is_cram
    )
    temp_needed, final_needed = get_free_space_needed(file_size, sort_type, is_cram)
    needed = temp_needed + final_needed
    if not check_free_space(outdir, needed) and not region:
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} (needs ~{needed} GB)."
        )


def cmd_identify(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    _require_input(args)
    logging.debug(f"Input file: {os.path.abspath(args.input)}")

    # Auto-resolve ref if a directory was provided
    resolved_ref = resolve_reference(args.ref, "")
    logging.debug(f"Resolved reference: {resolved_ref}")

    md5_sig = calculate_bam_md5(args.input, resolved_ref)
    logging.info(
        LOG_MESSAGES["ref_md5_signature"].format(input=args.input, sig=md5_sig)
    )


def cmd_sort(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    threads, memory, outdir, cram_opt, resolved_ref = get_base_args(args)
    region = resolve_region_or_gene(args, resolved_ref)

    _preflight_sort_space(args.input, outdir, "Coord", "Coord Sort", region)
    print_warning("GenSortedBAM", threads=threads)

    fmt, ext = _alignment_format(args.input)
    out_file = derive_output_path(args.input, outdir, f"_sorted{ext}")
    view_cmd = ["samtools", "view", "-uh", "--no-PG", *cram_opt, args.input]
    view_cmd += [region] if region else []

    logging.info(LOG_MESSAGES["sorting_file"].format(input=args.input, output=out_file))
    with (
        cleanup_files(on_failure=[out_file]),
        tempfile.TemporaryDirectory(dir=outdir, prefix=".sort_") as tempdir,
    ):
        sort_cmd = get_sam_sort_cmd(
            out_file, threads, memory, fmt=fmt, reference=resolved_ref, temp_dir=tempdir
        )
        run_pipeline([view_cmd, sort_cmd], description="Coordinate sort")


def cmd_index(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    _require_input(args)

    print_warning("GenBAMIndex")

    logging.info(LOG_MESSAGES["indexing_file"].format(path=args.input))
    try:
        run_command(get_sam_index_cmd(args.input))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Indexing failed: {e}") from e


def cmd_unindex(args: argparse.Namespace) -> None:
    _require_input(args)
    if not os.path.isfile(args.input):
        raise WGSExtractError(f"Input is not a valid file: {args.input}")

    for index_path in (args.input + ".bai", args.input + ".crai"):
        if os.path.exists(index_path):
            os.remove(index_path)
            logging.info(
                LOG_MESSAGES["delete_success"].format(
                    filename=os.path.basename(index_path)
                )
            )


def cmd_unsort(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    _, _, outdir, cram_opt, _ = get_base_args(args)

    _, ext = _alignment_format(args.input)
    out_file = derive_output_path(args.input, outdir, f"_unsorted{ext}")
    logging.info(
        LOG_MESSAGES["converting_file"].format(input=args.input, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            header = run_command(
                ["samtools", "view", "-H", *cram_opt, args.input], capture_output=True
            ).stdout
            with tempfile.TemporaryDirectory() as tempdir:
                newhead = os.path.join(tempdir, "newhead.sam")
                with open(newhead, "w") as f:
                    f.write(header.replace("SO:coordinate", "SO:unsorted"))
                with open(out_file, "wb") as f_out:
                    run_command(
                        ["samtools", "reheader", newhead, args.input], stdout=f_out
                    )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Failed to unsort {args.input}: {e}") from e


def cmd_tocram(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    threads, _, outdir, cram_opt, resolved_ref = get_base_args(args)
    region = resolve_region_or_gene(args, resolved_ref)

    print_warning("BAMtoCRAM", threads=threads)

    out_file = derive_output_path(args.input, outdir, ".cram")
    logging.info(
        LOG_MESSAGES["converting_file"].format(input=args.input, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            run_command(
                [
                    "samtools",
                    "view",
                    "-Ch",
                    "--output-fmt-option",
                    f"version={args.cram_version}",
                    *cram_opt,
                    "-@",
                    threads,
                    "-o",
                    out_file,
                    args.input,
                    *([region] if region else []),
                ]
            )
            run_command(get_sam_index_cmd(out_file))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Conversion to CRAM failed: {e}") from e


def cmd_tobam(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    threads, _, outdir, cram_opt, resolved_ref = get_base_args(args)
    region = resolve_region_or_gene(args, resolved_ref)

    print_warning("CRAMtoBAM", threads=threads)

    out_file = derive_output_path(args.input, outdir, ".bam")
    logging.info(
        LOG_MESSAGES["converting_file"].format(input=args.input, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            run_command(
                ["samtools", "view", "-bh", *cram_opt, "-@", threads, "-o", out_file]
                + [args.input, *([region] if region else [])]
            )
            run_command(get_sam_index_cmd(out_file))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Conversion to BAM failed: {e}") from e


def cmd_unalign(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    threads, memory, outdir, cram_opt, resolved_ref = get_base_args(args)
    region = resolve_region_or_gene(args, resolved_ref)

    _preflight_sort_space(args.input, outdir, "Name", "Name Sort", region)
    print_warning("ButtonUnalignBAM", threads=threads)

    def in_outdir(path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(outdir, path)

    logging.info(LOG_MESSAGES["unaligning_reads"])
    alignments_to_fastq(
        args.input,
        in_outdir(args.r1),
        in_outdir(args.r2),
        threads=threads,
        memory=memory,
        temp_parent=outdir,
        cram_opt=cram_opt,
        region=region,
        out_other=in_outdir(args.se) if args.se else None,
    )
