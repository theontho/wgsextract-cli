"""
(Re)align an existing BAM/CRAM to a reference genome.

Workflow:
  1. Resolve FASTQ inputs:
     - If ``--r1`` (and optionally ``--r2``) is provided, use those.
     - Otherwise look next to the input BAM and in the output directory for
       previously extracted FASTQ files (common naming patterns: ``<base>_R1.fastq.gz``, ``<base>.r1.fq.gz``,
       etc). If found, reuse them rather than re-extracting.
     - Otherwise extract them with ``samtools fastq``.
  2. Pre-flight resource check: estimate disk space for both the extraction
     stage (if needed) and the alignment stage, and refuse to start unless
     enough free space is available at the output directory. This avoids the
     classic failure mode of running an alignment for hours only to crash at
     the sort stage because of disk pressure.
  3. Delegate to the existing align command.
"""

import argparse
import contextlib
import logging
from pathlib import Path

from wgsextract_cli.core import bwa_mem2
from wgsextract_cli.core.alignment_memory import check_mapping_memory
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.read_counts import fastq_input_reads
from wgsextract_cli.core.reference_assets import (
    find_fasta_by_signature,
    input_stem,
    resolve_reference_library,
)
from wgsextract_cli.core.utils import WGSExtractError, get_resource_defaults
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import (
    check_free_space,
    get_free_space_needed,
    indexed_read_count,
)

from . import align as align_cmd
from ._extract_helpers import alignments_to_fastq

# Patterns used to locate previously extracted FASTQ files next to a BAM/CRAM.
# Each entry is (suffix_for_r1, suffix_for_r2). Order is preference.
_FASTQ_PATTERNS: list[tuple[str, str]] = [
    ("_R1.fastq.gz", "_R2.fastq.gz"),
    ("_R1.fq.gz", "_R2.fq.gz"),
    ("_r1.fastq.gz", "_r2.fastq.gz"),
    ("_r1.fq.gz", "_r2.fq.gz"),
    (".R1.fastq.gz", ".R2.fastq.gz"),
    (".r1.fq.gz", ".r2.fq.gz"),
    ("_1.fastq.gz", "_2.fastq.gz"),
    ("_1.fq.gz", "_2.fq.gz"),
]


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "realign",
        parents=[base_parser],
        help=CLI_HELP["cmd_realign"],
    )
    parser.add_argument("--r1", help=CLI_HELP["arg_r1"])
    parser.add_argument("--r2", help=CLI_HELP["arg_r2"])
    parser.add_argument(
        "--aligner",
        choices=align_cmd.ALIGNERS,
        default="auto",
        help="Aligner to use (matches the 'align' command).",
    )
    align_cmd.add_gpu_argument(parser)
    parser.add_argument(
        "--platform",
        choices=align_cmd.PLATFORMS,
        help="Sequencing platform (default: ultima when the input's read group "
        "says so, else illumina).",
    )
    parser.add_argument(
        "--preset",
        choices=["sr", "map-pb", "map-hifi", "map-ont", "ccs", "subread"],
        help="minimap2/pbmm2 preset.",
    )
    parser.add_argument(
        "--input-ref",
        help="Reference FASTA the input CRAM was encoded with (default: the FASTA "
        "in the reference library whose sequence dictionary matches the CRAM). "
        "--ref is the reference to realign to.",
    )
    parser.add_argument("--sample", help="Sample name to embed in read-group metadata.")
    parser.add_argument(
        "--long-read",
        action="store_true",
        help="Treat input as long-read data (PacBio/ONT).",
    )
    parser.add_argument(
        "--format",
        choices=["BAM", "CRAM"],
        default="BAM",
        help="Output format.",
    )
    parser.add_argument(
        "--keep-fastq",
        action="store_true",
        help="Keep the FASTQs extracted from --input after alignment (default: "
        "delete them). FASTQs passed with --r1/--r2 or reused from an earlier "
        "extraction are never deleted.",
    )
    parser.add_argument(
        "--force-extract",
        action="store_true",
        help="Re-extract FASTQs even if a matching pair already exists "
        "next to the BAM.",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip the up-front disk space check (not recommended).",
    )
    parser.set_defaults(func=run)


def _find_existing_fastqs(input_path: str, outdir: str) -> tuple[str, str] | None:
    """Look beside ``input_path`` and in ``outdir`` for an extracted R1/R2 pair."""
    base = input_stem(input_path)
    bam_dir = str(absolute_path(input_path).parent)
    for directory in dict.fromkeys([bam_dir, str(absolute_path(outdir))]):
        for r1_suffix, r2_suffix in _FASTQ_PATTERNS:
            r1 = str(Path(directory, base + r1_suffix))
            r2 = str(Path(directory, base + r2_suffix))
            if Path(r1).is_file() and Path(r2).is_file():
                return r1, r2
    return None


def _preflight_disk_space(
    input_path: str, outdir: str, need_extract: bool, reads: int | None
) -> None:
    """Estimate the peak disk usage and bail out if outdir lacks space."""
    file_size = Path(input_path).stat().st_size
    is_cram = input_path.lower().endswith(".cram")

    extract_temp_gb, extract_final_gb = (0, 0)
    if need_extract:
        extract_temp_gb, extract_final_gb = get_free_space_needed(
            file_size, sort_type="Name", is_cram=is_cram, reads=reads
        )

    align_temp_gb, align_final_gb = get_free_space_needed(
        file_size, sort_type="Coord", is_cram=is_cram, reads=reads
    )

    # Extraction's temp files are gone before alignment; its FASTQs are not.
    total_gb = max(extract_temp_gb, extract_final_gb + align_temp_gb + align_final_gb)
    logging.warning(
        "Realign pre-flight: ~%d GB free space required at %s "
        "(extract %d+%d, align %d+%d).",
        total_gb,
        outdir,
        extract_temp_gb,
        extract_final_gb,
        align_temp_gb,
        align_final_gb,
    )
    if not check_free_space(outdir, total_gb):
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} for realign "
            f"(needs ~{total_gb} GB). Free up space or rerun with "
            "--no-preflight to override (not recommended)."
        )


def _input_cram_reference(args: argparse.Namespace) -> list[str]:
    """samtools -T option for decoding a CRAM input; empty for BAM."""
    if not args.input.lower().endswith(".cram"):
        return []
    if args.input_ref:
        verify_paths_exist({"--input-ref": args.input_ref})
        return ["-T", args.input_ref]
    reflib = resolve_reference_library()
    signature = calculate_bam_md5(args.input)
    fasta = find_fasta_by_signature([reflib, str(Path(reflib, "genomes"))], signature)
    if not fasta:
        raise WGSExtractError(
            f"No FASTA in the reference library ({reflib}) matches the sequence "
            "dictionary of the input CRAM. Pass the reference it was encoded with "
            "via --input-ref."
        )
    logging.info("Decoding input CRAM with %s", fasta)
    return ["-T", fasta]


def run(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(
            "--input <BAM|CRAM> is required for realign. Pass it before the "
            "subcommand or via --input."
        )
    verify_paths_exist({"--input": args.input})
    args.platform = align_cmd.resolve_platform(args, args.r1 or args.input)
    # Reject bad aligner/platform/preset combinations before hours of extraction.
    align_cmd.select_aligner(args)
    signature = None
    if args.aligner == "bwa-mem2" and not (args.ref and Path(args.ref).is_file()):
        signature = calculate_bam_md5(args.input, None)
    target = resolve_reference(args.ref, signature) if args.ref or signature else None
    if args.aligner == "bwa-mem2" and target and Path(target).is_file():
        bwa_mem2.check_index(target)

    threads, memory = get_resource_defaults(args.threads, args.memory)
    outdir = args.outdir or str(absolute_path(args.input).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)

    if args.platform == "ultima" and not args.r1:
        # Single-end reads with flow tags: streamed into the aligner, no FASTQs.
        reads = indexed_read_count(args.input, threads)
        if not args.no_preflight:
            _preflight_disk_space(
                args.input,
                outdir,
                need_extract=False,
                reads=reads,
            )
        align_args = argparse.Namespace(**vars(args))
        align_args.r1 = args.input
        align_args._input_read_count = reads
        align_cmd.align_ultima(align_args, args.input, _input_cram_reference(args))
        return

    # 1. Resolve FASTQs.
    if args.r1:
        verify_paths_exist({"--r1": args.r1})
        if args.r2:
            verify_paths_exist({"--r2": args.r2})
        r1_path, r2_path = args.r1, args.r2
        need_extract = False
    elif not args.force_extract and (
        existing := _find_existing_fastqs(args.input, outdir)
    ):
        r1_path, r2_path = existing
        need_extract = False
        logging.info("Reusing existing FASTQs:\n  R1: %s\n  R2: %s", r1_path, r2_path)
    else:
        base = input_stem(args.input)
        r1_path = str(Path(outdir, f"{base}_R1.fastq.gz"))
        r2_path = str(Path(outdir, f"{base}_R2.fastq.gz"))
        need_extract = True

    reads = (
        indexed_read_count(args.input, threads)
        if need_extract
        else fastq_input_reads(r1_path, r2_path)
    )
    if args.aligner == "bwa-mem2" and target and Path(target).is_file():
        check_mapping_memory(
            target,
            "bwa-mem2",
            threads=threads,
            memory=memory,
            samblaster=align_cmd.duplicate_marker(paired=bool(r2_path)) == "samblaster",
            reads=reads,
        )

    # 2. Disk-space pre-flight.
    if not args.no_preflight:
        _preflight_disk_space(
            args.input, outdir, need_extract=need_extract, reads=reads
        )

    # 3. Extract if needed.
    if need_extract:
        verify_dependencies(["samtools"])
        log_dependency_info(["samtools"])
        logging.info("Extracting FASTQs from %s ...", args.input)
        try:
            alignments_to_fastq(
                args.input,
                r1_path,
                r2_path,
                threads=threads,
                memory=memory,
                temp_parent=outdir,
                cram_opt=_input_cram_reference(args),
            )
        except BaseException:
            # Never leave truncated FASTQs behind for a later run to "reuse".
            for path in (r1_path, r2_path):
                with contextlib.suppress(FileNotFoundError):
                    Path(path).unlink()
            raise

    # 4. Delegate to align.
    align_args = argparse.Namespace(**vars(args))
    align_args.r1 = r1_path
    align_args.r2 = r2_path
    align_args._input_read_count = reads
    align_cmd.run(align_args)

    # 5. Remove the FASTQs this run extracted unless asked to keep them. They are
    # kept when alignment fails so a rerun can reuse them.
    if need_extract and not args.keep_fastq:
        for path in (r1_path, r2_path):
            try:
                Path(path).unlink()
            except OSError as exc:
                logging.warning("Could not remove %s: %s", path, exc)
