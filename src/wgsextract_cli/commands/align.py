import argparse
import logging
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genome_library import pair_stem
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import is_fasta_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
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
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import print_warning

from ._extract_helpers import cleanup_files

_PACBIO_PLATFORMS = {"pacbio", "hifi", "clr"}
_LONG_READ_PLATFORMS = _PACBIO_PLATFORMS | {"ont", "nanopore"}
# pbmm2-style preset names accepted by minimap2 under their minimap2 names.
_MINIMAP2_PRESET_ALIASES = {"ccs": "map-hifi", "subread": "map-pb"}
_PBMM2_PRESETS = {
    "map-hifi": "CCS",
    "ccs": "CCS",
    "map-pb": "SUBREAD",
    "subread": "SUBREAD",
}
_READ_FILE_SUFFIXES = (
    ".fastq.gz",
    ".fq.gz",
    ".fastq",
    ".fq",
    ".subreads.bam",
    ".ccs.bam",
    ".bam",
)


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
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


def select_aligner(args: argparse.Namespace) -> str:
    """Resolve --aligner auto and reject nonsensical aligner/platform/preset combos."""
    long_read = args.long_read or args.platform in _LONG_READ_PLATFORMS
    aligner: str = args.aligner
    if aligner == "auto":
        if args.platform in _PACBIO_PLATFORMS and get_tool_path("pbmm2") is not None:
            aligner = "pbmm2"
        elif long_read or args.preset:
            aligner = "minimap2"
        else:
            aligner = "bwa"

    if aligner == "bwa":
        if long_read:
            raise WGSExtractError(
                "BWA-MEM is a short-read aligner; use --aligner minimap2 "
                "(or pbmm2 for PacBio) for long reads."
            )
        if args.preset:
            raise WGSExtractError("--preset applies to minimap2 and pbmm2, not BWA.")
    elif aligner == "pbmm2":
        if args.platform in {"ont", "nanopore"}:
            raise WGSExtractError(
                "pbmm2 only aligns PacBio reads; use --aligner minimap2 for ONT."
            )
        if args.preset and args.preset not in _PBMM2_PRESETS:
            raise WGSExtractError(
                "PacBio pbmm2 alignment supports --preset map-hifi, map-pb, ccs, "
                "or subread."
            )
    return aligner


def run(args: argparse.Namespace) -> None:
    aligner = select_aligner(args)
    if aligner == "pbmm2":
        align_pbmm2(args)
    elif aligner == "minimap2":
        align_minimap2(args)
    else:
        align_bwa(args)


def reads_stem(path: str) -> str:
    """File name of a FASTQ/unaligned-BAM read file without its read-file suffix."""
    name = Path(path).name
    for suffix in _READ_FILE_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


class _AlignSetup(NamedTuple):
    threads: str
    memory: str
    outdir: str
    ref: str
    stem: str


def _require_unambiguous_library(ref: str | None, task: str) -> None:
    """FASTQs don't say which genome they belong to, so a reference library
    holding several can't be picked from silently (it would be the first
    known name, e.g. hg19 for hg38 reads)."""
    if ref and Path(ref).is_file():
        return
    root = ReferenceLibrary(ref, skip_full_search=True).root
    if not root or not Path(root).is_dir():
        return
    fastas = sorted(
        str(Path(d, name))
        for d in (root, str(Path(root, "genomes")))
        if Path(d).is_dir()
        for name in os.listdir(d)
        if is_fasta_path(name)
    )
    if len(fastas) > 1:
        listed = "\n  ".join(fastas)
        raise WGSExtractError(
            f"{task} needs the genome FASTA to align to: pass --ref <FASTA>. "
            f"The reference library has {len(fastas)}:\n  {listed}"
        )


def _prepare_alignment(
    args: argparse.Namespace, tools: list[str], task: str
) -> _AlignSetup:
    verify_dependencies(tools)
    log_dependency_info(tools)
    threads, memory = get_resource_defaults(args.threads, args.memory)

    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves read inputs.")
    paths_to_check = {"--r1": args.r1}
    if args.r2:
        paths_to_check["--r2"] = args.r2
    verify_paths_exist(paths_to_check)

    input_path = args.input or args.r1
    outdir = args.outdir or str(absolute_path(input_path).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Input file: {str(absolute_path(input_path))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    md5_sig = calculate_bam_md5(input_path, None) if args.input else None
    if not md5_sig:
        _require_unambiguous_library(args.ref, task)
    resolved_ref = resolve_reference(args.ref, md5_sig)
    if not resolved_ref or not Path(resolved_ref).is_file():
        raise WGSExtractError(LOG_MESSAGES["ref_required_for"].format(task=task))
    logging.info(f"Aligning to {resolved_ref}")

    stem = reads_stem(args.r1)
    if getattr(args, "r2", None):
        # Both mates go into one output, so it shouldn't be named after R1.
        stem = pair_stem(stem)
    return _AlignSetup(threads, memory, outdir, resolved_ref, stem)


def ensure_bwa_index(ref: str) -> None:
    if Path(ref + ".bwt").exists():
        return
    logging.info(f"BWA index missing for {ref}. Generating now (may take a while)...")
    try:
        run_command(["bwa", "index", ref])
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Automatic BWA indexing failed: {e}") from e


def use_samblaster(paired: bool) -> bool:
    """samblaster marks duplicates in paired short reads; it aborts on unpaired input."""
    return paired and get_tool_path("samblaster") is not None


def run_streamed_alignment(
    align_cmd: list[str],
    out_path: str,
    *,
    threads: str,
    memory: str,
    fmt: str,
    ref: str,
    mark_duplicates: bool,
) -> None:
    """Run aligner [| samblaster] | samtools sort, then index the output."""
    stages = [align_cmd]
    if mark_duplicates:
        logging.info("Using samblaster for marking duplicates...")
        stages.append(["samblaster"])
    stages.append(
        get_sam_sort_cmd(
            out_path, threads, memory, fmt=fmt, reference=ref, sam_input=True
        )
    )
    partial = [out_path, out_path + ".bai", out_path + ".crai"]
    with cleanup_files(on_failure=partial):
        run_pipeline(stages, description="Alignment")
        logging.info(LOG_MESSAGES["indexing_output"])
        run_command(get_sam_index_cmd(out_path, threads=threads))


def align_bwa(args: argparse.Namespace) -> None:
    setup = _prepare_alignment(args, ["bwa", "samtools"], "BWA alignment")
    ensure_bwa_index(setup.ref)

    print_warning("ButtonBWAAlign", threads=setup.threads)

    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_bam = str(Path(setup.outdir, f"{setup.stem}_aligned{ext}"))

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(input=args.r1, output=out_bam, tool="BWA")
    )
    align_cmd = ["bwa", "mem", "-t", setup.threads, setup.ref, args.r1]
    try:
        run_streamed_alignment(
            align_cmd + ([args.r2] if args.r2 else []),
            out_bam,
            threads=setup.threads,
            memory=setup.memory,
            fmt=args.format,
            ref=setup.ref,
            mark_duplicates=use_samblaster(paired=bool(args.r2)),
        )
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"BWA alignment failed: {e}") from e


def _minimap2_preset(args: argparse.Namespace) -> str:
    if args.preset:
        preset: str = args.preset
        return _MINIMAP2_PRESET_ALIASES.get(preset, preset)
    if args.platform in {"hifi", "pacbio"}:
        return "map-hifi"
    if args.platform in {"ont", "nanopore"}:
        return "map-ont"
    if args.platform == "clr" or args.long_read:
        return "map-pb"
    return "sr"


def align_minimap2(args: argparse.Namespace) -> None:
    setup = _prepare_alignment(args, ["minimap2", "samtools"], "Minimap2 alignment")

    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_bam = str(Path(setup.outdir, f"{setup.stem}_aligned{ext}"))

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(
            input=args.r1, output=out_bam, tool="Minimap2"
        )
    )
    preset = _minimap2_preset(args)
    align_cmd = ["minimap2", "-ax", preset, "-t", setup.threads, setup.ref, args.r1]
    try:
        run_streamed_alignment(
            align_cmd + ([args.r2] if args.r2 else []),
            out_bam,
            threads=setup.threads,
            memory=setup.memory,
            fmt=args.format,
            ref=setup.ref,
            mark_duplicates=preset == "sr" and use_samblaster(paired=bool(args.r2)),
        )
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Minimap2 alignment failed: {e}") from e


def align_pbmm2(args: argparse.Namespace) -> None:
    if args.r2:
        raise WGSExtractError(
            "PacBio alignment expects single-end BAM/FASTQ input; do not pass --r2."
        )
    if args.format == "CRAM":
        raise WGSExtractError(
            "pbmm2 writes sorted BAM. Convert to CRAM with 'bam to-cram' after alignment."
        )
    setup = _prepare_alignment(args, ["pbmm2", "samtools"], "PacBio alignment")

    preset_arg = args.preset or ("subread" if args.platform == "clr" else "ccs")
    if preset_arg not in _PBMM2_PRESETS:
        raise WGSExtractError(
            "PacBio pbmm2 alignment supports --preset map-hifi, map-pb, ccs, or subread."
        )
    preset = _PBMM2_PRESETS[preset_arg]

    out_bam = str(Path(setup.outdir, f"{setup.stem}_aligned.bam"))
    sample = args.sample or setup.stem.split(".")[0]

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(
            input=args.r1, output=out_bam, tool="pbmm2"
        )
    )
    cmd = ["pbmm2", "align", setup.ref, args.r1, out_bam, "--sort"]
    cmd += ["--preset", preset, "--sample", sample, "-j", setup.threads]
    if preset == "SUBREAD":
        cmd.append("--median-filter")
    if args.r1.lower().endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz")):
        cmd.extend(["--rg", f"@RG\\tID:{setup.stem}\\tSM:{sample}"])
    try:
        run_command(cmd)
        logging.info(LOG_MESSAGES["indexing_output"])
        run_command(get_sam_index_cmd(out_bam, threads=setup.threads))
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"PacBio alignment failed: {e}") from e
