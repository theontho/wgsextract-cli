import argparse
import logging
import subprocess
import tempfile
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.resource_estimates import (
    estimate_resources,
    preflight_resources,
)
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    get_sam_index_cmd,
    get_sam_sort_cmd,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_bam_header,
    is_sorted,
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import (
    check_free_space,
    get_free_space_needed,
    indexed_read_count,
    print_warning,
)

from ._bam_ubam import UBAM_PLATFORMS, cmd_to_ubam
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
    # Output FASTQs: a bare file name goes in --outdir.
    unalign_parser.add_argument(
        "--r1",
        required=True,
        help="Read 1 FASTQ to write (a bare name goes in --outdir)",
    )
    unalign_parser.add_argument(
        "--r2",
        required=True,
        help="Read 2 FASTQ to write (a bare name goes in --outdir)",
    )
    unalign_parser.add_argument(
        "--se", help="FASTQ for reads without a mate (a bare name goes in --outdir)"
    )
    unalign_parser.add_argument("-r", "--region", help=CLI_HELP["arg_region"])
    unalign_parser.add_argument(
        "--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)"
    )
    unalign_parser.set_defaults(func=cmd_unalign)

    identify_parser = bam_subs.add_parser(
        "identify", parents=[base_parser], help=CLI_HELP["cmd_bam-identify"]
    )
    identify_parser.set_defaults(func=cmd_identify)

    merge_parser = bam_subs.add_parser(
        "merge", parents=[base_parser], help=CLI_HELP["cmd_bam-merge"]
    )
    merge_parser.add_argument(
        "inputs",
        nargs="+",
        metavar="FILE",
        help="Coordinate-sorted BAM/CRAM files aligned to the same reference.",
    )
    merge_parser.add_argument(
        "--output",
        help="Output file name or path (a bare name goes in --outdir; "
        "default: merged.bam, or merged.cram with --cram).",
    )
    merge_parser.add_argument(
        "--cram", action="store_true", help="Write CRAM instead of BAM."
    )
    merge_parser.add_argument(
        "--attach-rg",
        action="store_true",
        help="Tag each read with a read group named after its input file, for "
        "inputs without @RG lines (so reads stay traceable to their lane).",
    )
    merge_parser.set_defaults(func=cmd_merge)

    ubam_parser = bam_subs.add_parser(
        "to-ubam", parents=[base_parser], help=CLI_HELP["cmd_bam-to-ubam"]
    )
    ubam_parser.add_argument(
        "--r1", help="Read 1 FASTQ (instead of an aligned --input BAM/CRAM)."
    )
    ubam_parser.add_argument("--r2", help="Read 2 FASTQ for paired-end --r1.")
    ubam_parser.add_argument(
        "--sample",
        help="Sample name (SM) for FASTQ input, or for a BAM/CRAM without @RG "
        "lines (default: the input file name).",
    )
    ubam_parser.add_argument(
        "--library", default="lib1", help="Library name (LB) for a new read group."
    )
    ubam_parser.add_argument(
        "--platform",
        choices=list(UBAM_PLATFORMS),
        help="Sequencing platform (PL) for a new read group (default: detected "
        "from the read names).",
    )
    ubam_parser.set_defaults(func=cmd_to_ubam)


def _require_input(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})


def _alignment_format(path: str) -> tuple[str, str]:
    """Return (samtools format name, file extension) matching an input file."""
    return ("CRAM", ".cram") if path.lower().endswith(".cram") else ("BAM", ".bam")


def _preflight_sort_space(
    input_path: str,
    outdir: str,
    sort_type: str,
    app_name: str,
    region: str | None,
    action_key: str,
    threads: str,
) -> None:
    """Warn about, or refuse, a whole-file sort that cannot fit in outdir.

    The space and time estimates are for the whole file, so a region skips them.
    A name sort is done by `samtools collate`, which deletes each temp file as it
    writes that part of the output, so it peaks at the larger of the two.
    """
    if region:
        return
    temp_needed, final_needed = get_free_space_needed(
        Path(input_path).stat().st_size,
        sort_type,
        is_cram=input_path.lower().endswith(".cram"),
        reads=indexed_read_count(input_path, threads),
    )
    print_warning(
        "infoFreeSpace", app_name=app_name, size_gb=temp_needed, final_gb=final_needed
    )
    if sort_type == "Name":
        needed = max(temp_needed, final_needed)
    else:
        needed = temp_needed + final_needed
    if not check_free_space(outdir, needed):
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} (needs ~{needed} GB)."
        )
    print_warning(action_key, threads=threads)


def cmd_identify(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    _require_input(args)
    logging.debug(f"Input file: {str(absolute_path(args.input))}")

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

    fmt, ext = _alignment_format(args.input)
    preflight_resources(
        lambda: estimate_resources(
            "sort",
            input_paths=[args.input],
            output_dir=outdir,
            threads=threads,
            memory=memory,
            output_format=fmt,
            region=region,
        )
    )
    _preflight_sort_space(
        args.input, outdir, "Coord", "Coord Sort", region, "GenSortedBAM", threads
    )

    out_file = derive_output_path(args.input, outdir, f"_sorted{ext}")
    view_cmd = ["samtools", "view", "-uh", "--no-PG", *cram_opt, args.input]
    view_cmd += [region] if region else []

    logging.info(LOG_MESSAGES["sorting_file"].format(input=args.input, output=out_file))
    # sambamba writes the index beside the BAM as it sorts.
    partial = [out_file, *(out_file + ext for ext in _INDEX_SUFFIXES)]
    with (
        cleanup_files(on_failure=partial),
        tempfile.TemporaryDirectory(dir=outdir, prefix=".sort_") as tempdir,
    ):
        sort_cmd = get_sam_sort_cmd(
            out_file, threads, memory, fmt=fmt, reference=resolved_ref, temp_dir=tempdir
        )
        run_pipeline([view_cmd, sort_cmd], description="Coordinate sort")


_INDEX_SUFFIXES = (".bai", ".crai", ".csi")


def _index_files(alignment: str) -> list[Path]:
    """Every index file samtools would use for an alignment: s.bam.bai, s.bai,
    s.bam.csi, s.cram.crai, s.crai."""
    path = Path(alignment)
    return [
        *(path.with_name(path.name + ext) for ext in _INDEX_SUFFIXES),
        path.with_suffix(".bai"),
        path.with_suffix(".crai"),
    ]


def cmd_index(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    _require_input(args)

    print_warning("GenBAMIndex")

    logging.info(LOG_MESSAGES["indexing_file"].format(path=args.input))
    before = {p: p.stat().st_mtime_ns for p in _index_files(args.input) if p.exists()}
    try:
        run_command(get_sam_index_cmd(args.input))
    except (OSError, subprocess.SubprocessError) as e:
        # sambamba writes a .bai before it finds the BAM unsorted; a corrupt
        # index would read as "Indexed" afterwards.
        for path in _index_files(args.input):
            if path.exists() and before.get(path) != path.stat().st_mtime_ns:
                path.unlink()
        raise WGSExtractError(f"Indexing failed: {e}") from e


def cmd_unindex(args: argparse.Namespace) -> None:
    _require_input(args)
    if not Path(args.input).is_file():
        raise WGSExtractError(f"Input is not a valid file: {args.input}")

    for index_path in _index_files(args.input):
        if index_path.exists():
            index_path.unlink()
            logging.info(
                LOG_MESSAGES["delete_success"].format(filename=index_path.name)
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
                newhead = str(Path(tempdir, "newhead.sam"))
                with open(newhead, "w", newline="\n") as f:
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

    preflight_resources(
        lambda: estimate_resources(
            "to-cram",
            input_paths=[args.input],
            output_dir=outdir,
            threads=threads,
            output_format="CRAM",
            region=region,
        )
    )
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

    preflight_resources(
        lambda: estimate_resources(
            "to-bam",
            input_paths=[args.input],
            output_dir=outdir,
            threads=threads,
            output_format="BAM",
            region=region,
        )
    )
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


def _fastq_output_path(path: str, outdir: str) -> str:
    """A bare file name goes in --outdir; a path with a directory is used as given."""
    return str(Path(outdir, path)) if Path(path).name == path else path


def cmd_unalign(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    threads, memory, outdir, cram_opt, resolved_ref = get_base_args(args)
    region = resolve_region_or_gene(args, resolved_ref)

    _preflight_sort_space(
        args.input, outdir, "Name", "Name Sort", region, "ButtonUnalignBAM", threads
    )

    read_groups = _read_groups(get_bam_header(args.input) or "")
    if len(read_groups) > 1:
        # FASTQ has nowhere to keep them; aligning the FASTQs makes one group.
        logging.warning(
            f"{args.input} has {len(read_groups)} read groups "
            f"({', '.join(read_groups)}); the FASTQs mix them. Use "
            "'wgsextract bam to-ubam' to keep each read's group."
        )

    logging.info(LOG_MESSAGES["unaligning_reads"])
    alignments_to_fastq(
        args.input,
        _fastq_output_path(args.r1, outdir),
        _fastq_output_path(args.r2, outdir),
        threads=threads,
        memory=memory,
        temp_parent=outdir,
        cram_opt=cram_opt,
        region=region,
        out_other=_fastq_output_path(args.se, outdir) if args.se else None,
    )


def _merge_output_path(args: argparse.Namespace, outdir: str) -> str:
    ext = ".cram" if args.cram else ".bam"
    name = args.output or f"merged{ext}"
    if not name.lower().endswith((".bam", ".cram")):
        name += ext
    elif name.lower().endswith(".cram") != args.cram:
        raise WGSExtractError(
            f"--output {name} does not match the output format "
            f"({'CRAM' if args.cram else 'BAM'}); add or drop --cram."
        )
    return _fastq_output_path(name, outdir)


def _sequence_dictionary(header: str) -> tuple[tuple[str, str], ...]:
    """Ordered (SN, LN) pairs from the @SQ lines of a SAM header."""
    entries = []
    for line in header.splitlines():
        if line.startswith("@SQ"):
            fields = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
            entries.append((fields.get("SN", ""), fields.get("LN", "")))
    return tuple(entries)


def _read_groups(header: str) -> dict[str, str]:
    """@RG lines keyed by ID."""
    groups = {}
    for line in header.splitlines():
        if line.startswith("@RG"):
            for field in line.split("\t")[1:]:
                if field.startswith("ID:"):
                    groups[field[3:]] = line
    return groups


def _is_cram(path: str) -> bool:
    return path.lower().endswith(".cram")


def cmd_merge(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    inputs: list[str] = args.inputs
    if len(inputs) < 2:
        raise WGSExtractError("bam merge needs at least two input files.")
    verify_paths_exist({f"input {i}": path for i, path in enumerate(inputs, 1)})
    if len({str(Path(p).resolve()) for p in inputs}) != len(inputs):
        raise WGSExtractError("The same file is listed more than once.")

    first_header = ""
    first_dict: tuple[tuple[str, str], ...] = ()
    read_groups: dict[str, set[str]] = {}
    for path in inputs:
        header = get_bam_header(path)
        if not header:
            raise WGSExtractError(f"Could not read the header of {path}.")
        if not is_sorted(path, header=header):
            raise WGSExtractError(
                f"{path} is not coordinate-sorted; run 'wgsextract bam sort' first."
            )
        for rg_id, line in _read_groups(header).items():
            read_groups.setdefault(rg_id, set()).add(line)
        seq_dict = _sequence_dictionary(header)
        if not first_header:
            first_header, first_dict = header, seq_dict
        elif seq_dict != first_dict:
            raise WGSExtractError(
                f"{path} has a different sequence dictionary (@SQ lines) than "
                f"{inputs[0]}; merge only files aligned to the same reference."
            )

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir or str(absolute_path(inputs[0]).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    out_file = _merge_output_path(args, outdir)
    if any(Path(out_file).resolve() == Path(p).resolve() for p in inputs):
        raise WGSExtractError(f"Output {out_file} would overwrite an input file.")

    ref = None
    if args.cram or any(_is_cram(p) for p in inputs):
        ref = resolve_reference(
            args.ref, calculate_bam_md5(inputs[0], header=first_header), inputs[0]
        )
        if not (ref and Path(ref).is_file()):
            raise WGSExtractError(
                LOG_MESSAGES["ref_required_for"].format(task="merging CRAM files")
            )

    # Output is about the size of the inputs; CRAM is roughly half of BAM.
    out_bytes = 0.0
    for path in inputs:
        ratio = 1.0 if _is_cram(path) == args.cram else (0.5 if args.cram else 2.0)
        out_bytes += Path(path).stat().st_size * ratio
    needed_gb = int(out_bytes / 1024**3) + 1
    if not check_free_space(outdir, needed_gb):
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} (needs ~{needed_gb} GB)."
        )

    # Name the index explicitly: samtools would otherwise write .csi for BAM,
    # which GATK/htsjdk tools cannot read.
    index = out_file + (".crai" if args.cram else ".bai")
    # -f: replace an earlier result like the other commands do. (samtools'
    # exists check doesn't see through ##idx##, but don't rely on that.)
    cmd = ["samtools", "merge", "-f", "-@", threads, "--write-index"]
    cmd += ["-O", "CRAM" if args.cram else "BAM", "-o", f"{out_file}##idx##{index}"]
    if ref:
        cmd += ["--reference", ref]
    if args.attach_rg:
        cmd.append("-r")
    conflicting = sorted(i for i, lines in read_groups.items() if len(lines) > 1)
    if conflicting:
        logging.warning(
            f"Read group ID(s) {', '.join(conflicting)} are defined differently in "
            "different inputs; samtools will give them distinct IDs."
        )
    else:
        # Identical @RG lines are the same read group split across files.
        cmd.append("-c")
    logging.info(f"Merging {len(inputs)} files into {out_file}...")
    try:
        with cleanup_files(on_failure=[out_file, index]):
            run_command(cmd + inputs, capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        stderr = getattr(e, "stderr", None)
        raise WGSExtractError(
            f"Merging failed: {e}" + (f"\n{stderr.strip()}" if stderr else "")
        ) from e
    logging.info(f"Merged file written to {out_file}")
    logging.info(
        "If the inputs are lanes of one library, mark duplicates again on the "
        "merged file before variant calling."
    )
