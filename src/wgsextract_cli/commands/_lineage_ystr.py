import argparse
import logging
from pathlib import Path

from wgsextract_cli.core.builds import build_from_chr1_length, build_from_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import calculate_bam_md5, verify_paths_exist

from ._lineage_ydna import _yleaf_build

# A male sample at even 5x calls most markers.
NO_Y_FRACTION = 0.1


def cmd_ystr(args: argparse.Namespace) -> None:
    from wgsextract_cli.ydna.ystr.analysis import (
        STATUS_TEXT,
        analyze,
        header_build,
        header_lengths,
    )
    from wgsextract_cli.ydna.ystr.panel import select

    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})
    is_cram = args.input.lower().endswith(".cram")
    if not is_cram and not args.input.lower().endswith(".bam"):
        raise WGSExtractError(
            "Y-STR calling needs reads: pass an indexed BAM or CRAM. "
            "Align FASTQ with `wgsextract align` first."
        )
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])

    outdir = absolute_path(args.outdir or str(Path(args.input).absolute().parent))
    ystr_outdir = Path(outdir, "ystr")

    lib = ReferenceLibrary(
        args.ref, calculate_bam_md5(args.input, None), input_path=args.input
    )
    # The header's chrY length identifies the build without a reference library.
    build = _yleaf_build(
        getattr(args, "build", None)
        or lib.build
        or header_build(header_lengths(Path(args.input)))
    )
    if is_cram and not lib.fasta:
        raise WGSExtractError(
            "A reference FASTA is needed to decode CRAM; pass it with --ref."
        )
    if lib.fasta:
        reference_build = build_from_chr1_length(
            read_fai_lengths(lib.fasta)
        ) or build_from_path(lib.fasta)
        if is_cram and reference_build and reference_build != build:
            raise WGSExtractError(
                f"The selected reference FASTA is {reference_build}, but the "
                f"input's build is {build}. Pass the FASTA the CRAM was aligned "
                "to with --ref, or set --build."
            )

    selected = select(args.panel)
    calls, table = analyze(
        Path(args.input),
        ystr_outdir,
        build=build,
        selected=selected,
        reference=Path(lib.fasta) if lib.fasta else None,
        min_reads=args.min_reads,
        min_fraction=args.min_fraction,
        min_mapq=args.min_mapq,
    )

    called = [c for c in calls if c.status == "called"]
    # Checked from the calls rather than idxstats, which reads a whole CRAM.
    if len(called) < NO_Y_FRACTION * len(calls):
        raise WGSExtractError(
            f"Only {len(called)} of {len(calls)} Y-STRs could be called: "
            f"{args.input} has too few chrY reads (the sample may be female, or "
            f"the build may be wrong). Details are in {table}."
        )
    print(f"\n🧬 Y-STR {args.panel} (build {build}): {len(called)}/{len(calls)} called")
    print("-" * 52)
    print(f"{'Marker':<13}{'Value':>8}  {'Reads':>5}  Note")
    for c in calls:
        note = STATUS_TEXT[c.status]
        if c.status == "called" and not c.marker.value_verified:
            note = "unverified FTDNA convention"
        reads = f"{c.support}/{c.spanning}" if c.spanning else ""
        print(f"{c.marker.name:<13}{c.value or '-':>8}  {reads:>5}  {note}")
    print("-" * 52)
    unverified = sum(not c.marker.value_verified for c in called)
    if unverified:
        logging.warning(
            f"{unverified} called markers have a reference repeat count that no "
            "authoritative source confirms; their values may be offset from "
            "FamilyTreeDNA's convention (see value_convention in the table)."
        )
    print(
        f"Results: {table}\nFTDNA-order CSV: {table.with_suffix('').with_suffix('.ftdna.csv')}\n"
    )


def add_ystr_parser(
    lin_subs: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
    help_text: str,
) -> None:
    from wgsextract_cli.ydna.ystr.panel import PANELS

    parser = lin_subs.add_parser("y-str", parents=[base_parser], help=help_text)
    parser.add_argument(
        "--panel",
        choices=PANELS,
        default="Y-111",
        help="Marker panel: FamilyTreeDNA Y-12 to Y-111, PowerPlex Y23, "
        "Yfiler Plus, or all (default: Y-111).",
    )
    parser.add_argument(
        "--build",
        choices=["hg19", "hg38", "t2t"],
        help="Override the detected reference build for this run.",
    )
    parser.add_argument(
        "--min-reads",
        type=int,
        default=2,
        help="Spanning reads needed to support a repeat count (default: 2).",
    )
    parser.add_argument(
        "--min-fraction",
        type=float,
        default=0.6,
        help="Share of spanning reads that must agree (default: 0.6).",
    )
    parser.add_argument(
        "--min-mapq",
        type=int,
        default=20,
        help="Minimum mapping quality for single-copy markers (default: 20).",
    )
    parser.set_defaults(func=cmd_ystr)
