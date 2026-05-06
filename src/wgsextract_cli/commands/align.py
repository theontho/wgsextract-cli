import logging
import os
import subprocess
from pathlib import Path

from wgsextract_cli.core.dependencies import (
    get_tool_path,
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    calculate_bam_md5,
    get_resource_defaults,
    get_sam_index_cmd,
    popen,
    resolve_reference,
    run_command,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import print_warning


def register(subparsers, base_parser):
    parser = subparsers.add_parser(
        "align", parents=[base_parser], help=CLI_HELP["cmd_align"]
    )
    parser.add_argument("--r1", help=CLI_HELP["arg_r1"])
    parser.add_argument("--r2", help=CLI_HELP["arg_r2"])
    parser.add_argument(
        "--preset",
        choices=["sr", "map-pb", "map-hifi", "map-ont", "ccs", "subread"],
        help="minimap2 preset for non-BWA alignment. Defaults to map-hifi for PacBio and map-ont for ONT.",
    )
    parser.add_argument(
        "--platform",
        choices=["illumina", "pacbio", "hifi", "clr", "ont", "nanopore"],
        default="illumina",
        help="Sequencing platform; selects platform-specific alignment tooling.",
    )
    parser.add_argument(
        "--aligner",
        choices=["auto", "bwa", "minimap2", "pbmm2"],
        default="auto",
        help="Aligner to use. Auto uses pbmm2 for PacBio when installed, otherwise minimap2.",
    )
    parser.add_argument(
        "--sample", help="Sample name to write into read-group metadata when supported."
    )
    parser.add_argument(
        "--long-read", action="store_true", help=CLI_HELP["arg_long_read"]
    )
    parser.add_argument(
        "--format", choices=["BAM", "CRAM"], default="BAM", help="Output format"
    )
    parser.set_defaults(func=run)


def run(args):
    # Determine which aligner to use
    platform = getattr(args, "platform", "illumina")
    aligner = getattr(args, "aligner", "auto")
    if aligner == "pbmm2" or (
        aligner == "auto"
        and platform in {"pacbio", "hifi", "clr"}
        and get_tool_path("pbmm2") is not None
    ):
        align_pbmm2(args)
    elif (
        aligner == "minimap2"
        or args.long_read
        or platform in {"pacbio", "hifi", "clr", "ont", "nanopore"}
    ):
        align_minimap2(args)
    elif aligner == "bwa" or aligner == "auto":
        align_bwa(args)
    else:
        raise WGSExtractError(f"Unsupported aligner: {aligner}")


def _aligner_stream_sort_cmd(out_bam, threads, memory, fmt, reference):
    cmd = ["samtools", "sort", "-@", threads, "-m", memory, "-o", out_bam]
    if fmt == "CRAM":
        cmd += ["-O", "CRAM", "--reference", reference]
    else:
        cmd += ["-O", "BAM"]
    return cmd


def align_bwa(args):
    verify_dependencies(["bwa", "samtools"])
    log_dependency_info(["bwa", "samtools"])
    threads, memory = get_resource_defaults(args.threads, args.memory)

    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves FASTQ inputs.")

    # Use --input's path if outdir not set, or r1's path
    input_path = args.input if args.input else args.r1
    logging.debug(f"Input file: {os.path.abspath(input_path)}")

    paths_to_check = {"--r1": args.r1}
    if args.r2:
        paths_to_check["--r2"] = args.r2
    if not verify_paths_exist(paths_to_check):
        return

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_path))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    md5_sig = calculate_bam_md5(input_path, None) if args.input else None
    resolved_ref = resolve_reference(args.ref, md5_sig)
    logging.debug(f"Resolved reference: {resolved_ref}")

    if not resolved_ref or not os.path.isfile(resolved_ref):
        raise WGSExtractError(
            LOG_MESSAGES["ref_required_for"].format(task="BWA alignment")
        )

    # Check for BWA index files, if missing, run indexing
    bwt_index = resolved_ref + ".bwt"
    if not os.path.exists(bwt_index):
        logging.info(
            f"BWA index missing for {resolved_ref}. Generating now (may take a while)..."
        )
        try:
            bwa = get_tool_path("bwa")
            run_command([bwa, "index", resolved_ref])
        except Exception as e:
            logging.error(f"Automatic indexing failed: {e}")
            raise WGSExtractError("Automatic BWA indexing failed.") from e

    print_warning("ButtonBWAAlign", threads=threads)

    base_name = os.path.basename(args.r1).split(".")[0]
    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_bam = os.path.join(outdir, f"{base_name}_aligned{ext}")

    r2_args = [args.r2] if args.r2 else []

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(input=args.r1, output=out_bam, tool="BWA")
    )
    try:
        # Resolve tools
        bwa = get_tool_path("bwa")
        samblaster = get_tool_path("samblaster")

        # 1. Aligner command
        align_cmd = [bwa, "mem", "-t", threads, resolved_ref, args.r1] + r2_args

        # 2. Mark duplicates (optional)
        use_blaster = samblaster is not None
        if use_blaster:
            logging.info("Using samblaster for marking duplicates...")

        # 3. Sorter command
        sort_cmd = _aligner_stream_sort_cmd(
            out_bam, threads, memory, args.format, resolved_ref
        )

        # Pipe align -> [samblaster] -> sort
        p_align = popen(align_cmd, stdout=subprocess.PIPE)

        if use_blaster:
            p_blaster = popen(
                [samblaster], stdin=p_align.stdout, stdout=subprocess.PIPE
            )
            if p_align.stdout:
                p_align.stdout.close()
            p_sort = popen(sort_cmd, stdin=p_blaster.stdout)
            if p_blaster.stdout:
                p_blaster.stdout.close()
        else:
            p_sort = popen(sort_cmd, stdin=p_align.stdout)
            if p_align.stdout:
                p_align.stdout.close()

        p_sort.communicate()

        logging.info(LOG_MESSAGES["indexing_output"])
        index_cmd = get_sam_index_cmd(out_bam, threads=threads)
        run_command(index_cmd)
    except Exception as e:
        logging.error(f"BWA alignment failed: {e}")
        raise WGSExtractError("BWA alignment failed.") from e


def align_minimap2(args):
    verify_dependencies(["minimap2", "samtools"])
    log_dependency_info(["minimap2", "samtools"])
    threads, memory = get_resource_defaults(args.threads, args.memory)

    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves FASTQ inputs.")

    input_path = args.input if args.input else args.r1
    paths_to_check = {"--r1": args.r1}
    if args.r2:
        paths_to_check["--r2"] = args.r2
    if not verify_paths_exist(paths_to_check):
        return

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_path))
    )
    logging.debug(f"Input file: {os.path.abspath(input_path)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    md5_sig = calculate_bam_md5(input_path, None) if args.input else None
    resolved_ref = resolve_reference(args.ref, md5_sig)
    logging.debug(f"Resolved reference: {resolved_ref}")

    if not resolved_ref or not os.path.isfile(resolved_ref):
        raise WGSExtractError(
            LOG_MESSAGES["ref_required_for"].format(task="Minimap2 alignment")
        )

    base_name = os.path.basename(args.r1).split(".")[0]
    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_bam = os.path.join(outdir, f"{base_name}_aligned{ext}")

    r2_args = [args.r2] if args.r2 else []

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(
            input=args.r1, output=out_bam, tool="Minimap2"
        )
    )
    try:
        # Resolve tools
        minimap2 = get_tool_path("minimap2")
        samblaster = get_tool_path("samblaster")

        preset = getattr(args, "preset", None)
        if not preset:
            platform = getattr(args, "platform", "illumina")
            if platform in {"hifi", "pacbio"}:
                preset = "map-hifi"
            elif platform in {"ont", "nanopore"}:
                preset = "map-ont"
            elif platform == "clr" or getattr(args, "long_read", False):
                preset = "map-pb"
            else:
                preset = "sr"

        # 1. Aligner command
        align_cmd = [
            minimap2,
            "-ax",
            preset,
            "-t",
            threads,
            resolved_ref,
            args.r1,
        ] + r2_args

        # 2. Mark duplicates (optional)
        platform = getattr(args, "platform", "illumina")
        use_blaster = (
            samblaster is not None
            and not getattr(args, "long_read", False)
            and platform not in {"pacbio", "hifi", "clr", "ont", "nanopore"}
        )
        if use_blaster:
            logging.info("Using samblaster for marking duplicates...")

        # 3. Sorter command
        sort_cmd = _aligner_stream_sort_cmd(
            out_bam, threads, memory, args.format, resolved_ref
        )

        # Pipe align -> [samblaster] -> sort
        p_align = popen(align_cmd, stdout=subprocess.PIPE)

        if use_blaster:
            p_blaster = popen(
                [samblaster], stdin=p_align.stdout, stdout=subprocess.PIPE
            )
            if p_align.stdout:
                p_align.stdout.close()
            p_sort = popen(sort_cmd, stdin=p_blaster.stdout)
            if p_blaster.stdout:
                p_blaster.stdout.close()
        else:
            p_sort = popen(sort_cmd, stdin=p_align.stdout)
            if p_align.stdout:
                p_align.stdout.close()

        p_sort.communicate()

        logging.info(LOG_MESSAGES["indexing_output"])
        index_cmd = get_sam_index_cmd(out_bam, threads=threads)
        run_command(index_cmd)
    except Exception as e:
        logging.error(f"Minimap2 alignment failed: {e}")
        raise WGSExtractError("Minimap2 alignment failed.") from e


def align_pbmm2(args):
    verify_dependencies(["pbmm2", "samtools"])
    log_dependency_info(["pbmm2", "samtools"])
    threads, _memory = get_resource_defaults(args.threads, args.memory)

    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves PacBio reads.")
    if args.r2:
        raise WGSExtractError(
            "PacBio alignment expects single-end BAM/FASTQ input; do not pass --r2."
        )
    if args.format == "CRAM":
        raise WGSExtractError(
            "pbmm2 writes sorted BAM. Convert to CRAM with 'bam to-cram' after alignment."
        )

    input_path = args.input if args.input else args.r1
    if not verify_paths_exist({"--r1": args.r1}):
        return

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_path))
    )
    logging.debug(f"Input file: {os.path.abspath(input_path)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    md5_sig = calculate_bam_md5(input_path, None) if args.input else None
    resolved_ref = resolve_reference(args.ref, md5_sig)
    logging.debug(f"Resolved reference: {resolved_ref}")

    if not resolved_ref or not os.path.isfile(resolved_ref):
        raise WGSExtractError(
            LOG_MESSAGES["ref_required_for"].format(task="PacBio alignment")
        )

    platform = getattr(args, "platform", "pacbio")
    preset = getattr(args, "preset", None) or (
        "subread" if platform == "clr" else "ccs"
    )
    preset = preset.upper()
    if preset == "MAP-PB":
        preset = "SUBREAD"
    elif preset == "MAP-HIFI":
        preset = "CCS"
    elif preset not in {"CCS", "SUBREAD"}:
        raise WGSExtractError(
            "PacBio pbmm2 alignment supports --preset map-hifi, map-pb, ccs, or subread."
        )

    base_name = Path(args.r1).name
    for suffix in (
        ".fastq.gz",
        ".fq.gz",
        ".fastq",
        ".fq",
        ".subreads.bam",
        ".ccs.bam",
        ".bam",
    ):
        if base_name.endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break
    out_bam = os.path.join(outdir, f"{base_name}_aligned.bam")
    sample = getattr(args, "sample", None) or base_name.split(".")[0]

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(
            input=args.r1, output=out_bam, tool="pbmm2"
        )
    )
    try:
        pbmm2 = get_tool_path("pbmm2")
        cmd = [
            pbmm2,
            "align",
            resolved_ref,
            args.r1,
            out_bam,
            "--sort",
            "--preset",
            preset,
            "--sample",
            sample,
            "-j",
            threads,
        ]
        if preset == "SUBREAD":
            cmd.append("--median-filter")
        if args.r1.lower().endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz")):
            cmd.extend(["--rg", f"@RG\\tID:{base_name}\\tSM:{sample}"])
        run_command(cmd)

        logging.info(LOG_MESSAGES["indexing_output"])
        index_cmd = get_sam_index_cmd(out_bam, threads=threads)
        run_command(index_cmd)
    except Exception as e:
        logging.error(f"pbmm2 alignment failed: {e}")
        raise WGSExtractError("PacBio alignment failed.") from e
