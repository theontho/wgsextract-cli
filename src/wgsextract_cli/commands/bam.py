import argparse
import logging
import subprocess
import tempfile
from os import fspath
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.bam import (
    BamIdentifyParams,
    BamIndexParams,
    BamMergeParams,
    BamSortParams,
    BamToBamParams,
    BamToCramParams,
    BamToUbamParams,
    BamUnalignParams,
    BamUnindexParams,
    BamUnsortParams,
    _InputParams,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.output_checks import record_alignment_output
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.resource_estimates import (
    estimate_resources,
    preflight_resources,
)
from wgsextract_cli.core.results import current_result, warn
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.temp_directory import configured_temp_directory
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_sam_index_cmd,
    get_sam_sort_cmd,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_bam_header,
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import (
    check_free_space,
    get_free_space_needed,
    indexed_read_count,
    print_warning,
)

from ._bam_merge import run_merge
from ._bam_ubam import cmd_to_ubam
from ._extract_helpers import (
    BaseArgs,
    alignments_to_fastq,
    base_args,
    cleanup_files,
    derive_output_path,
    named_output_path,
    region_or_gene,
)


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser("bam", help=CLI_HELP["cmd_bam_mgmt"])
    bam_subs = parser.add_subparsers(dest="bam_cmd", required=True)
    for name, cls, func, help_key in (
        ("sort", BamSortParams, cmd_sort, "cmd_sort"),
        ("index", BamIndexParams, cmd_index, "cmd_index"),
        ("unindex", BamUnindexParams, cmd_unindex, "cmd_unindex"),
        ("unsort", BamUnsortParams, cmd_unsort, "cmd_unsort"),
        ("to-cram", BamToCramParams, cmd_tocram, "cmd_to-cram"),
        ("to-bam", BamToBamParams, cmd_tobam, "cmd_to-bam"),
        ("unalign", BamUnalignParams, cmd_unalign, "cmd_unalign"),
        ("identify", BamIdentifyParams, cmd_identify, "cmd_bam-identify"),
        ("merge", BamMergeParams, cmd_merge, "cmd_bam-merge"),
        ("to-ubam", BamToUbamParams, cmd_to_ubam, "cmd_bam-to-ubam"),
    ):
        sub = bam_subs.add_parser(name, parents=[base_parser], help=CLI_HELP[help_key])
        add_arguments(sub, cls)
        sub.set_defaults(func=func)


def _require_input(params: _InputParams) -> str:
    input_path = fspath(params.input)
    verify_paths_exist({"--input": input_path})
    return input_path


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
    scratch = configured_temp_directory()
    requirements = (
        [(scratch, temp_needed), (outdir, final_needed)]
        if scratch and Path(scratch).stat().st_dev != Path(outdir).stat().st_dev
        else [(outdir, needed)]
    )
    for directory, gigabytes in requirements:
        if not check_free_space(directory, gigabytes):
            raise WGSExtractError(
                f"Insufficient free disk space at {directory} (needs ~{gigabytes} GB)."
            )
    print_warning(action_key, threads=threads)


def _setup(
    params: BamSortParams | BamToCramParams | BamToBamParams | BamUnalignParams,
) -> tuple[str, BaseArgs, str | None]:
    """The input, its resources and reference, and the region or gene's
    coordinates."""
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    base = base_args(
        params.input,
        ref=params.ref,
        outdir=params.outdir,
        threads=params.threads,
        memory=getattr(params, "memory", None),
        region=params.region,
    )
    region = region_or_gene(params.region, params.gene, params.input, base.ref)
    return fspath(params.input), base, region


def run_identify(params: BamIdentifyParams) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    input_path = _require_input(params)
    logging.debug(f"Input file: {str(absolute_path(input_path))}")

    # Auto-resolve ref if a directory was provided
    resolved_ref = resolve_reference(fspath(params.ref) if params.ref else None, "")
    logging.debug(f"Resolved reference: {resolved_ref}")

    md5_sig = calculate_bam_md5(input_path, resolved_ref)
    current_result().add_metrics(reference_md5_signature=md5_sig)
    logging.info(
        LOG_MESSAGES["ref_md5_signature"].format(input=input_path, sig=md5_sig)
    )


def run_sort(params: BamSortParams) -> None:
    input_path, base, region = _setup(params)
    threads, memory, outdir, cram_opt, resolved_ref = base

    fmt, ext = _alignment_format(input_path)
    preflight_resources(
        lambda: estimate_resources(
            "sort",
            input_paths=[input_path],
            output_dir=outdir,
            temp_dir=configured_temp_directory(),
            threads=threads,
            memory=memory,
            output_format=fmt,
            region=region,
        )
    )
    _preflight_sort_space(
        input_path, outdir, "Coord", "Coord Sort", region, "GenSortedBAM", threads
    )

    out_file = derive_output_path(input_path, outdir, f"_sorted{ext}")
    view_cmd = ["samtools", "view", "-uh", "--no-PG", *cram_opt, input_path]
    view_cmd += [region] if region else []

    logging.info(LOG_MESSAGES["sorting_file"].format(input=input_path, output=out_file))
    # An index left by an earlier run would describe the old file: drop it, so
    # the one used below is the one sambamba writes as it sorts, or ours.
    indexes = _index_files(out_file)
    for index in indexes:
        index.unlink(missing_ok=True)
    partial = [out_file, *map(str, indexes)]
    with (
        cleanup_files(on_failure=partial),
        tempfile.TemporaryDirectory(
            dir=configured_temp_directory() or outdir, prefix=".sort_"
        ) as tempdir,
    ):
        sort_cmd = get_sam_sort_cmd(
            out_file, threads, memory, fmt=fmt, reference=resolved_ref, temp_dir=tempdir
        )
        run_pipeline([view_cmd, sort_cmd], description="Coordinate sort")
        if not alignment_index(out_file):  # samtools sort, unlike sambamba
            run_command(get_sam_index_cmd(out_file))
    current_result().set_reference(resolved_ref)
    current_result().add_metrics(
        **record_alignment_output(out_file, "sorted_alignment")
    )


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


def run_index(params: BamIndexParams) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    input_path = _require_input(params)

    print_warning("GenBAMIndex")

    logging.info(LOG_MESSAGES["indexing_file"].format(path=input_path))
    before = {p: p.stat().st_mtime_ns for p in _index_files(input_path) if p.exists()}
    try:
        run_command(get_sam_index_cmd(input_path))
    except (OSError, subprocess.SubprocessError) as e:
        # sambamba writes a .bai before it finds the BAM unsorted; a corrupt
        # index would read as "Indexed" afterwards.
        for path in _index_files(input_path):
            if path.exists() and before.get(path) != path.stat().st_mtime_ns:
                path.unlink()
        raise WGSExtractError(f"Indexing failed: {e}") from e
    for path in _index_files(input_path):
        if path.is_file() and before.get(path) != path.stat().st_mtime_ns:
            current_result().add_output(path, "alignment_index")


def run_unindex(params: BamUnindexParams) -> None:
    input_path = _require_input(params)
    if not Path(input_path).is_file():
        raise WGSExtractError(f"Input is not a valid file: {input_path}")

    removed = 0
    for index_path in _index_files(input_path):
        if index_path.exists():
            index_path.unlink()
            removed += 1
            logging.info(
                LOG_MESSAGES["delete_success"].format(filename=index_path.name)
            )
    current_result().add_metrics(removed_indexes=removed)


def run_unsort(params: BamUnsortParams) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    input_path = fspath(params.input)
    _, _, outdir, cram_opt, _ = base_args(
        input_path, ref=params.ref, outdir=params.outdir, threads=None, memory=None
    )

    _, ext = _alignment_format(input_path)
    out_file = derive_output_path(input_path, outdir, f"_unsorted{ext}")
    logging.info(
        LOG_MESSAGES["converting_file"].format(input=input_path, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            header = run_command(
                ["samtools", "view", "-H", *cram_opt, input_path], capture_output=True
            ).stdout
            with tempfile.TemporaryDirectory() as tempdir:
                newhead = str(Path(tempdir, "newhead.sam"))
                with open(newhead, "w", newline="\n") as f:
                    f.write(header.replace("SO:coordinate", "SO:unsorted"))
                with open(out_file, "wb") as f_out:
                    run_command(
                        ["samtools", "reheader", newhead, input_path], stdout=f_out
                    )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Failed to unsort {input_path}: {e}") from e
    current_result().add_metrics(
        **record_alignment_output(out_file, "unsorted_alignment")
    )


def run_to_cram(params: BamToCramParams) -> None:
    input_path, base, region = _setup(params)
    threads, _, outdir, cram_opt, resolved_ref = base

    preflight_resources(
        lambda: estimate_resources(
            "to-cram",
            input_paths=[input_path],
            output_dir=outdir,
            threads=threads,
            output_format="CRAM",
            region=region,
        )
    )
    print_warning("BAMtoCRAM", threads=threads)

    out_file = derive_output_path(input_path, outdir, ".cram")
    logging.info(
        LOG_MESSAGES["converting_file"].format(input=input_path, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            run_command(
                [
                    "samtools",
                    "view",
                    "-Ch",
                    "--output-fmt-option",
                    f"version={params.cram_version}",
                    *cram_opt,
                    "-@",
                    threads,
                    "-o",
                    out_file,
                    input_path,
                    *([region] if region else []),
                ]
            )
            run_command(get_sam_index_cmd(out_file))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Conversion to CRAM failed: {e}") from e
    current_result().set_reference(resolved_ref)
    current_result().add_metrics(**record_alignment_output(out_file, "alignment"))


def run_to_bam(params: BamToBamParams) -> None:
    input_path, base, region = _setup(params)
    threads, _, outdir, cram_opt, resolved_ref = base

    preflight_resources(
        lambda: estimate_resources(
            "to-bam",
            input_paths=[input_path],
            output_dir=outdir,
            threads=threads,
            output_format="BAM",
            region=region,
        )
    )
    print_warning("CRAMtoBAM", threads=threads)

    out_file = derive_output_path(input_path, outdir, ".bam")
    logging.info(
        LOG_MESSAGES["converting_file"].format(input=input_path, output=out_file)
    )
    try:
        with cleanup_files(on_failure=[out_file]):
            run_command(
                ["samtools", "view", "-bh", *cram_opt, "-@", threads, "-o", out_file]
                + [input_path, *([region] if region else [])]
            )
            run_command(get_sam_index_cmd(out_file))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Conversion to BAM failed: {e}") from e
    current_result().set_reference(resolved_ref)
    current_result().add_metrics(**record_alignment_output(out_file, "alignment"))


def run_unalign(params: BamUnalignParams) -> None:
    input_path, base, region = _setup(params)
    threads, memory, outdir, cram_opt, resolved_ref = base

    _preflight_sort_space(
        input_path, outdir, "Name", "Name Sort", region, "ButtonUnalignBAM", threads
    )

    read_groups = _read_groups(get_bam_header(input_path) or "")
    if len(read_groups) > 1:
        # FASTQ has nowhere to keep them; aligning the FASTQs makes one group.
        warn(
            "read_groups_mixed",
            f"{input_path} has {len(read_groups)} read groups "
            f"({', '.join(read_groups)}); the FASTQs mix them. Use "
            "'wgsextract bam to-ubam' to keep each read's group.",
            read_groups=len(read_groups),
        )

    logging.info(LOG_MESSAGES["unaligning_reads"])
    fastqs = {
        "fastq_r1": named_output_path(params.r1, outdir),
        "fastq_r2": named_output_path(params.r2, outdir),
    }
    if params.se:
        fastqs["fastq_single"] = named_output_path(params.se, outdir)
    alignments_to_fastq(
        input_path,
        fastqs["fastq_r1"],
        fastqs["fastq_r2"],
        threads=threads,
        memory=memory,
        temp_parent=outdir,
        cram_opt=cram_opt,
        region=region,
        out_other=fastqs.get("fastq_single"),
    )
    for role, path in fastqs.items():
        current_result().add_output(path, role)
    current_result().add_metrics(read_groups=len(read_groups))


def _read_groups(header: str) -> dict[str, str]:
    """@RG lines keyed by ID."""
    groups = {}
    for line in header.splitlines():
        if line.startswith("@RG"):
            for field in line.split("\t")[1:]:
                if field.startswith("ID:"):
                    groups[field[3:]] = line
    return groups


cmd_sort = cli_handler(BamSortParams, run_sort)
cmd_index = cli_handler(BamIndexParams, run_index)
cmd_unindex = cli_handler(BamUnindexParams, run_unindex)
cmd_unsort = cli_handler(BamUnsortParams, run_unsort)
cmd_tocram = cli_handler(BamToCramParams, run_to_cram)
cmd_tobam = cli_handler(BamToBamParams, run_to_bam)
cmd_unalign = cli_handler(BamUnalignParams, run_unalign)
cmd_identify = cli_handler(BamIdentifyParams, run_identify)
cmd_merge = cli_handler(BamMergeParams, run_merge)
