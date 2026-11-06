import argparse
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.lineage import YStrParams
from wgsextract_cli.core.builds import build_from_chr1_length, build_from_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import current_result, human_output, warn
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import calculate_bam_md5, verify_paths_exist

from ._lineage_ydna import _yleaf_build

# A male sample at even 5x calls most markers.
NO_Y_FRACTION = 0.1


def run_y_str(params: YStrParams) -> None:
    """``lineage y-str``."""
    from wgsextract_cli.ydna.ystr.analysis import (
        STATUS_TEXT,
        analyze,
        header_build,
        header_lengths,
    )
    from wgsextract_cli.ydna.ystr.panel import select

    input_file = str(params.input)
    verify_paths_exist({"--input": input_file})
    is_cram = input_file.lower().endswith(".cram")
    if not is_cram and not input_file.lower().endswith(".bam"):
        raise WGSExtractError(
            "Y-STR calling needs reads: pass an indexed BAM or CRAM. "
            "Align FASTQ with `wgsextract align` first."
        )
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])

    outdir = absolute_path(str(params.outdir or Path(input_file).absolute().parent))
    ystr_outdir = Path(outdir, "ystr")

    lib = ReferenceLibrary(
        str(params.ref) if params.ref else None,
        calculate_bam_md5(input_file, None),
        input_path=input_file,
    )
    # The header's chrY length identifies the build without a reference library.
    build = _yleaf_build(
        params.build or lib.build or header_build(header_lengths(Path(input_file)))
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

    selected = select(params.panel)
    calls, table = analyze(
        Path(input_file),
        ystr_outdir,
        build=build,
        selected=selected,
        reference=Path(lib.fasta) if lib.fasta else None,
        min_reads=params.min_reads,
        min_fraction=params.min_fraction,
        min_mapq=params.min_mapq,
    )

    called = [c for c in calls if c.status == "called"]
    # Checked from the calls rather than idxstats, which reads a whole CRAM.
    if len(called) < NO_Y_FRACTION * len(calls):
        raise WGSExtractError(
            f"Only {len(called)} of {len(calls)} Y-STRs could be called: "
            f"{input_file} has too few chrY reads (the sample may be female, or "
            f"the build may be wrong). Details are in {table}."
        )
    ftdna_csv = table.with_suffix("").with_suffix(".ftdna.csv")
    result = current_result()
    result.set_reference(lib.fasta, build)
    result.add_output(table, "ystr_table")
    result.add_output(ftdna_csv, "ystr_ftdna_csv")
    result.add_metrics(panel=params.panel, markers=len(calls), called=len(called))
    result.set_details(
        {
            "build": build,
            "panel": params.panel,
            "calls": [
                {
                    "marker": c.marker.name,
                    "value": c.value or None,
                    "status": c.status,
                    "support": c.support,
                    "spanning": c.spanning,
                    "value_verified": c.marker.value_verified,
                }
                for c in calls
            ],
        }
    )
    unverified = sum(not c.marker.value_verified for c in called)
    if unverified:
        warn(
            "ystr_unverified_convention",
            f"{unverified} called markers have a reference repeat count that no "
            "authoritative source confirms; their values may be offset from "
            "FamilyTreeDNA's convention (see value_convention in the table).",
            markers=unverified,
        )
    human_output(
        f"\n🧬 Y-STR {params.panel} (build {build}): {len(called)}/{len(calls)} called"
    )
    human_output("-" * 52)
    human_output(f"{'Marker':<13}{'Value':>8}  {'Reads':>5}  Note")
    for c in calls:
        note = STATUS_TEXT[c.status]
        if c.status == "called" and not c.marker.value_verified:
            note = "unverified FTDNA convention"
        reads = f"{c.support}/{c.spanning}" if c.spanning else ""
        human_output(f"{c.marker.name:<13}{c.value or '-':>8}  {reads:>5}  {note}")
    human_output("-" * 52)
    human_output(f"Results: {table}\nFTDNA-order CSV: {ftdna_csv}\n")


cmd_ystr = cli_handler(YStrParams, run_y_str)


def add_ystr_parser(
    lin_subs: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
    help_text: str,
) -> None:
    parser = lin_subs.add_parser("y-str", parents=[base_parser], help=help_text)
    add_arguments(parser, YStrParams)
    parser.set_defaults(func=cmd_ystr)
