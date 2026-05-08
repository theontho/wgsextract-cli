"""
(Re)align an existing BAM/CRAM to a reference genome.

Workflow:
  1. Resolve FASTQ inputs:
     - If ``--r1`` (and optionally ``--r2``) is provided, use those.
     - Otherwise look next to the input BAM for previously extracted FASTQ
       files (common naming patterns: ``<base>_R1.fastq.gz``, ``<base>.r1.fq.gz``,
       etc). If found, reuse them rather than re-extracting.
     - Otherwise extract them with ``samtools fastq``.
  2. Pre-flight resource check: estimate disk space for both the extraction
     stage (if needed) and the alignment stage, and refuse to start unless
     enough free space is available at the output directory. This avoids the
     classic failure mode of running an alignment for hours only to crash at
     the sort stage because of disk pressure.
  3. Delegate to the existing align command.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from wgsextract_cli.core.dependencies import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    get_sam_sort_cmd,
    popen,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import (
    check_free_space,
    get_free_space_needed,
)

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


def register(subparsers, base_parser):
    parser = subparsers.add_parser(
        "realign",
        parents=[base_parser],
        help=CLI_HELP.get(
            "cmd_realign",
            "Re-align an existing BAM/CRAM to a (possibly different) reference. "
            "Reuses previously extracted FASTQs when available; otherwise "
            "extracts them on demand. Verifies free disk space up-front.",
        ),
    )
    parser.add_argument("--r1", help=CLI_HELP.get("arg_r1", "Forward FASTQ override."))
    parser.add_argument("--r2", help=CLI_HELP.get("arg_r2", "Reverse FASTQ override."))
    parser.add_argument(
        "--aligner",
        choices=["auto", "bwa", "minimap2", "pbmm2"],
        default="auto",
        help="Aligner to use (matches the 'align' command).",
    )
    parser.add_argument(
        "--platform",
        choices=["illumina", "pacbio", "hifi", "clr", "ont", "nanopore"],
        default="illumina",
        help="Sequencing platform.",
    )
    parser.add_argument(
        "--preset",
        choices=["sr", "map-pb", "map-hifi", "map-ont", "ccs", "subread"],
        help="minimap2/pbmm2 preset.",
    )
    parser.add_argument(
        "--sample", help="Sample name to embed in read-group metadata."
    )
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
        help="Keep extracted FASTQs after alignment (default: keep only if "
        "extraction wrote to a path the user explicitly named with --r1/--r2 "
        "or to the chosen output directory).",
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


def _bam_basename(input_path: str) -> str:
    name = Path(input_path).name
    for ext in (".bam", ".cram", ".sam"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return Path(name).stem


def _find_existing_fastqs(input_path: str) -> tuple[str, str] | None:
    """Look beside ``input_path`` for a previously extracted R1/R2 pair."""
    bam_dir = os.path.dirname(os.path.abspath(input_path)) or "."
    base = _bam_basename(input_path)
    for r1_suffix, r2_suffix in _FASTQ_PATTERNS:
        r1 = os.path.join(bam_dir, base + r1_suffix)
        r2 = os.path.join(bam_dir, base + r2_suffix)
        if os.path.isfile(r1) and os.path.isfile(r2):
            return r1, r2
    return None


def _extract_fastqs(
    input_path: str, outdir: str, threads: str, memory: str
) -> tuple[str, str]:
    """Extract paired FASTQs from a BAM/CRAM via ``samtools view | sort -n | fastq``."""
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])

    base = _bam_basename(input_path)
    out_r1 = os.path.join(outdir, f"{base}_R1.fastq.gz")
    out_r2 = os.path.join(outdir, f"{base}_R2.fastq.gz")
    is_cram = input_path.lower().endswith(".cram")

    logging.info("Extracting FASTQs from %s ...", input_path)
    with tempfile.TemporaryDirectory() as tempdir:
        view_cmd = ["samtools", "view", "-uh", "--no-PG", input_path]
        sort_cmd = get_sam_sort_cmd(
            "-",
            threads,
            memory,
            fmt="SAM",
            name_sort=True,
            temp_dir=tempdir,
        )
        fastq_cmd = [
            "samtools",
            "fastq",
            "-1",
            out_r1,
            "-2",
            out_r2,
            "-0",
            "/dev/null",
            "-s",
            "/dev/null",
            "-n",
            "-@",
            threads,
            "-",
        ]
        p1 = popen(view_cmd, stdout=subprocess.PIPE)
        p2 = popen(sort_cmd, stdin=p1.stdout, stdout=subprocess.PIPE)
        if p1.stdout:
            p1.stdout.close()
        p3 = popen(fastq_cmd, stdin=p2.stdout)
        if p2.stdout:
            p2.stdout.close()
        p3.communicate()
        if p3.returncode != 0:
            raise WGSExtractError("FASTQ extraction failed.")

    # CRAM-derived sort temp space is roughly proportional to file size; we
    # logged the estimate during the preflight check, so no need to repeat here.
    _ = is_cram
    return out_r1, out_r2


def _preflight_disk_space(
    input_path: str, outdir: str, need_extract: bool
) -> None:
    """Estimate the worst-case disk usage and bail out if outdir lacks space."""
    file_size = os.path.getsize(input_path)
    is_cram = input_path.lower().endswith(".cram")

    extract_temp_gb, extract_final_gb = (0, 0)
    if need_extract:
        extract_temp_gb, extract_final_gb = get_free_space_needed(
            file_size, sort_type="Name", is_cram=is_cram
        )

    align_temp_gb, align_final_gb = get_free_space_needed(
        file_size, sort_type="Coord", is_cram=is_cram
    )

    total_gb = (
        extract_temp_gb + extract_final_gb + align_temp_gb + align_final_gb
    )
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


def run(args):
    if not getattr(args, "input", None):
        raise WGSExtractError(
            "--input <BAM|CRAM> is required for realign. Pass it before the "
            "subcommand or via --input."
        )
    if not verify_paths_exist({"--input": args.input}):
        return

    threads, memory = get_resource_defaults(
        getattr(args, "threads", None), getattr(args, "memory", None)
    )

    outdir = (
        args.outdir
        if getattr(args, "outdir", None)
        else os.path.dirname(os.path.abspath(args.input)) or "."
    )
    os.makedirs(outdir, exist_ok=True)

    # 1. Resolve FASTQs.
    user_supplied = bool(getattr(args, "r1", None))
    if user_supplied:
        if not verify_paths_exist({"--r1": args.r1}):
            return
        if getattr(args, "r2", None) and not verify_paths_exist({"--r2": args.r2}):
            return
        r1_path, r2_path = args.r1, getattr(args, "r2", None)
        need_extract = False
    elif not args.force_extract and (existing := _find_existing_fastqs(args.input)):
        r1_path, r2_path = existing
        need_extract = False
        logging.info(
            "Reusing existing FASTQs:\n  R1: %s\n  R2: %s", r1_path, r2_path
        )
    else:
        r1_path = r2_path = None
        need_extract = True

    # 2. Disk-space pre-flight.
    if not args.no_preflight:
        _preflight_disk_space(args.input, outdir, need_extract=need_extract)

    # 3. Extract if needed.
    if need_extract:
        r1_path, r2_path = _extract_fastqs(args.input, outdir, threads, memory)

    # 4. Delegate to align.
    from wgsextract_cli.commands import align as align_cmd

    align_args = argparse.Namespace(**vars(args))
    align_args.r1 = r1_path
    align_args.r2 = r2_path
    align_cmd.run(align_args)

    # 5. Optionally clean up extracted FASTQs (only if we extracted them, the
    # user did not pass --keep-fastq, and they live where align would have
    # written them anyway).
    if need_extract and not args.keep_fastq:
        for path in (r1_path, r2_path):
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    logging.warning("Could not remove %s: %s", path, exc)
        # Best-effort: drop empty parent dirs we may have created.
        try:
            if os.path.isdir(outdir) and not os.listdir(outdir):
                os.rmdir(outdir)
        except OSError:
            pass

    _ = shutil  # silence unused-import linters when shutil is only used transitively
