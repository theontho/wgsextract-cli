import argparse
import logging
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from wgsextract_cli.core import bwa_mem2, ultima
from wgsextract_cli.core.alignment_memory import check_mapping_memory
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genome_library import pair_stem
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.read_counts import fastq_input_reads
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
from wgsextract_cli.core.warnings import (
    indexed_read_count,
    print_warning,
    warn_alignment_sort_space,
)

from ._extract_helpers import cleanup_files

PLATFORMS = ["illumina", "dnbseq", "ultima", "pacbio", "hifi", "clr", "ont", "nanopore"]
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
ALIGNERS = ["auto", "bwa", "bwa-mem2", "minimap2", "pbmm2"]
_BWA_ALIGNERS = {"bwa", "bwa-mem2"}
# BWA's default batch size depends on threads and changes pairing statistics.
BWA_BATCH_BASES = "100000000"
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
        choices=PLATFORMS,
        help="Sequencing platform; selects platform-specific alignment tooling "
        "(default: ultima for a BAM/CRAM whose read group says so, else illumina).",
    )
    parser.add_argument(
        "--aligner",
        choices=ALIGNERS,
        default="auto",
        help="Aligner to use. Auto uses bwa-mem2 for short reads when the reference "
        "has its index (see `ref index`), else bwa; for long reads pbmm2 for PacBio "
        "when installed, otherwise minimap2.",
    )
    add_gpu_argument(parser)
    parser.add_argument(
        "--sample",
        help="Sample name for the read group (default: the reads' file name).",
    )
    parser.add_argument(
        "--long-read", action="store_true", help=CLI_HELP["arg_long_read"]
    )
    parser.add_argument(
        "--format", choices=["BAM", "CRAM"], default="BAM", help="Output format"
    )
    parser.set_defaults(func=run)


def resolve_platform(args: argparse.Namespace, reads: str | None) -> str:
    """--platform, else ultima when the reads are a BAM/CRAM with an Ultima read
    group (FASTQ names don't say: SRA renumbers them), else illumina."""
    if args.platform:
        return str(args.platform)
    if reads and reads.lower().endswith((".bam", ".cram")) and Path(reads).is_file():
        if ultima.is_ultima_header(get_bam_header(reads)):
            logging.info(f"{reads} is from an Ultima sequencer: aligning it as such.")
            return "ultima"
    return "illumina"


def select_aligner(args: argparse.Namespace) -> str:
    """Resolve --aligner auto and reject nonsensical aligner/platform/preset combos."""
    long_read = args.long_read or args.platform in _LONG_READ_PLATFORMS
    aligner: str = args.aligner
    if args.platform == "ultima":
        if aligner not in ("auto", *_BWA_ALIGNERS) or long_read or args.preset:
            raise WGSExtractError(
                "Ultima reads align with BWA-MEM (--aligner auto, bwa or bwa-mem2), "
                "as Ultima's and Broad's pipelines do."
            )
        if getattr(args, "gpu", False):
            raise WGSExtractError(
                "--gpu (Parabricks) doesn't keep Ultima's flow tags (tp, t0) or "
                "mark flow-based duplicates; align Ultima reads without --gpu."
            )
        if getattr(args, "r2", None):
            raise WGSExtractError("Ultima reads are single-end: drop --r2.")
        return "bwa" if aligner == "auto" else aligner
    if aligner == "auto":
        if args.platform in _PACBIO_PLATFORMS and get_tool_path("pbmm2") is not None:
            aligner = "pbmm2"
        elif long_read or args.preset:
            aligner = "minimap2"
        else:
            aligner = "bwa"

    if aligner in _BWA_ALIGNERS:
        if long_read:
            raise WGSExtractError(
                "BWA-MEM is a short-read aligner; use --aligner minimap2 "
                "(or pbmm2 for PacBio) for long reads."
            )
        if args.preset:
            raise WGSExtractError("--preset applies to minimap2 and pbmm2, not BWA.")
        if aligner == "bwa-mem2" and getattr(args, "gpu", False):
            raise WGSExtractError(
                "--gpu aligns with Parabricks' own BWA-MEM; use --aligner bwa or auto."
            )
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


def add_gpu_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Align on an NVIDIA GPU with Parabricks: BWA-MEM for short reads, "
        "minimap2 for HiFi/ONT (Linux, Docker; see README).",
    )


# Parabricks minimap2 presets for this command's long-read presets.
_GPU_MINIMAP2_PRESETS = {"map-hifi": "map-hifi", "map-ont": "map-ont"}


def run(args: argparse.Namespace) -> None:
    args.platform = resolve_platform(args, args.r1)
    aligner = select_aligner(args)
    if args.platform == "ultima":
        align_ultima(args, args.r1)
        return
    if getattr(args, "gpu", False):
        _run_gpu(args, aligner)
        return
    if aligner == "pbmm2":
        align_pbmm2(args)
    elif aligner == "minimap2":
        align_minimap2(args)
    else:
        align_bwa(args)


def short_read_aligner(
    requested: str,
    ref: str,
    *,
    threads: str = "1",
    memory: str = "512M",
    samblaster: bool = True,
    reads: int | None = None,
) -> str:
    """bwa or bwa-mem2 for --aligner bwa, bwa-mem2 or auto. Auto takes bwa-mem2,
    whose alignments are bwa's in less time, when it's installed and the
    reference has its index that fits in this computer's memory."""
    if requested != "auto":
        return requested
    if get_tool_path("bwa-mem2") is None:
        return "bwa"
    fits = bwa_mem2.can_map(
        ref, threads=threads, memory=memory, samblaster=samblaster, reads=reads
    )
    if bwa_mem2.has_index(ref):
        if fits:
            return "bwa-mem2"
        logging.info(
            f"Not using bwa-mem2: the alignment pipeline needs about "
            f"{bwa_mem2.mapping_memory(ref, threads=threads, memory=memory, samblaster=samblaster, reads=reads) / 1024**3:.1f} "
            "GiB of available RAM. Use --aligner bwa or lower --memory."
        )
    elif fits:
        logging.info(
            "bwa-mem2 would align this about a third faster; "
            f"`wgsextract ref index --aligner bwa-mem2 --ref {ref}` sets it up."
        )
    return "bwa"


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


def alt_contigs(ref: str) -> list[str]:
    """The reference's ALT haplotype contigs (GRCh38's *_alt, and the HLA alleles
    in hs38DH), which repeat stretches of the primary chromosomes."""
    fai = Path(ref + ".fai")
    if not fai.is_file():
        run_command(["samtools", "faidx", ref])
    if not fai.is_file():
        return []
    names = [line.split("\t", 1)[0] for line in fai.read_text().splitlines()]
    return [n for n in names if n.endswith("_alt") or n.startswith("HLA-")]


def ensure_bwa_alt_file(ref: str) -> None:
    """Make BWA ALT-aware on a reference with ALT contigs. Without <ref>.alt,
    a read from a region that has an ALT copy aligns equally well to both and
    gets mapping quality 0, which variant callers count as no evidence: such
    regions lose their variants. BWA reads only the contig names from the file
    (an existing bwakit .alt, which also has their alignments, is kept)."""
    alt_file = Path(ref + ".alt")
    if alt_file.exists():
        return
    names = alt_contigs(ref)
    if not names:
        return
    try:
        alt_file.write_text("".join(f"{n}\n" for n in names), newline="\n")
    except OSError as e:
        raise WGSExtractError(
            f"{ref} has {len(names)} ALT contigs, and BWA needs {alt_file} listing "
            f"them to give reads in those regions a mapping quality: {e}"
        ) from e
    logging.info(f"Wrote {alt_file}: BWA maps ALT-aware over {len(names)} contigs.")


def duplicate_marker(paired: bool) -> str:
    """How short reads get their duplicates marked: samblaster in the stream
    when it's installed (it aborts on unpaired input), else samtools markdup,
    which samtools always provides."""
    if paired and get_tool_path("samblaster") is not None:
        return "samblaster"
    return "markdup"


def _sort_and_mark_stages(
    out_path: str, *, threads: str, memory: str, fmt: str, ref: str
) -> list[list[str]]:
    """samtools fixmate | sort | markdup, writing out_path. The sorted stream
    goes straight into markdup uncompressed, so the output is written once.
    fixmate reads the aligner's output directly: it keeps mates together."""
    tmp = f"{out_path}.tmp"
    out_fmt = ["-O", "CRAM", "--reference", ref] if fmt == "CRAM" else ["-O", "BAM"]
    return [
        ["samtools", "fixmate", "-m", "-u", "-", "-"],
        ["samtools", "sort", "-@", threads, "-m", memory, "-u", "-T", tmp, "-"],
        ["samtools", "markdup", "-@", threads, "-T", tmp, *out_fmt, "-", out_path],
    ]


def run_streamed_alignment(
    align_cmd: Sequence[object],
    out_path: str,
    *,
    threads: str,
    memory: str,
    fmt: str,
    ref: str,
    duplicates: str | None,
    upstream: list[Sequence[object]] | None = None,
) -> None:
    """Run the aligner (after any upstream stages feeding it), mark duplicates
    (with duplicate_marker's choice, or not at all for long reads: None), sort,
    then index the output."""
    stages: list[Sequence[object]] = [*(upstream or []), list(align_cmd)]
    sort = get_sam_sort_cmd(
        out_path, threads, memory, fmt=fmt, reference=ref, sam_input=True
    )
    if duplicates == "samblaster":
        logging.info("Using samblaster for marking duplicates...")
        stages += [["samblaster"], sort]
    elif duplicates == "markdup":
        logging.info("Marking duplicates with samtools markdup...")
        stages += _sort_and_mark_stages(
            out_path, threads=threads, memory=memory, fmt=fmt, ref=ref
        )
    else:
        stages.append(sort)
    partial = [out_path, out_path + ".bai", out_path + ".crai"]
    with cleanup_files(on_failure=partial):
        run_pipeline(stages, description="Alignment")
        logging.info(LOG_MESSAGES["indexing_output"])
        run_command(get_sam_index_cmd(out_path, threads=threads))


# SAM-spec @RG PL values for --platform.
_RG_PLATFORMS = {
    "illumina": "ILLUMINA",
    "dnbseq": "DNBSEQ",
    "pacbio": "PACBIO",
    "hifi": "PACBIO",
    "clr": "PACBIO",
    "ont": "ONT",
    "nanopore": "ONT",
    "ultima": ultima.PLATFORM,
}


def read_group(args: argparse.Namespace, stem: str) -> str:
    """The @RG line for -R (bwa, minimap2 expand the \\t). Variant callers and
    multi-sample tools need each sample's own SM, and GATK requires a read group
    with a platform. The default sample is the reads' name without its mate
    marker (family.mother_R1 -> family.mother), so different people stay apart."""
    sample = getattr(args, "sample", None) or pair_stem(stem)
    platform = _RG_PLATFORMS.get(getattr(args, "platform", None) or "", "ILLUMINA")
    return f"@RG\\tID:{stem}\\tSM:{sample}\\tLB:{sample}\\tPL:{platform}"


def _bwa_setup(args: argparse.Namespace, task: str) -> tuple[_AlignSetup, str]:
    """The alignment setup and bwa or bwa-mem2 (short_read_aligner's choice),
    with the reference's index and ALT contig list in place."""
    tool = "bwa-mem2" if args.aligner == "bwa-mem2" else "bwa"
    setup = _prepare_alignment(args, [tool, "samtools"], task)
    samblaster = bool(args.r2) and get_tool_path("samblaster") is not None
    if hasattr(args, "_input_read_count"):
        reads = args._input_read_count
    else:
        reads = (
            indexed_read_count(args.r1, setup.threads)
            if args.r1.lower().endswith((".bam", ".cram"))
            else fastq_input_reads(args.r1, args.r2)
        )
    tool = short_read_aligner(
        args.aligner,
        setup.ref,
        threads=setup.threads,
        memory=setup.memory,
        samblaster=samblaster,
        reads=reads,
    )
    check_mapping_memory(
        setup.ref,
        tool,
        threads=setup.threads,
        memory=setup.memory,
        samblaster=samblaster,
        reads=reads,
    )
    warn_alignment_sort_space(setup.outdir, reads)
    if tool == "bwa-mem2":
        bwa_mem2.ensure_index(setup.ref, download=False)
    else:
        ensure_bwa_index(setup.ref)
    ensure_bwa_alt_file(setup.ref)
    print_warning("ButtonBWAAlign", threads=setup.threads)
    return setup, tool


def align_bwa(args: argparse.Namespace) -> None:
    """BWA-MEM alignment with bwa or bwa-mem2 (short_read_aligner's choice)."""
    setup, tool = _bwa_setup(args, "BWA alignment")

    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_bam = str(Path(setup.outdir, f"{setup.stem}_aligned{ext}"))

    logging.info(
        LOG_MESSAGES["aligning_reads"].format(input=args.r1, output=out_bam, tool=tool)
    )
    align_cmd = [
        tool, "mem", "-K", BWA_BATCH_BASES, "-t", setup.threads,
        "-R", read_group(args, setup.stem),
    ]  # fmt: skip
    align_cmd += [setup.ref, args.r1]
    try:
        run_streamed_alignment(
            align_cmd + ([args.r2] if args.r2 else []),
            out_bam,
            threads=setup.threads,
            memory=setup.memory,
            fmt=args.format,
            ref=setup.ref,
            duplicates=duplicate_marker(paired=bool(args.r2)),
        )
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"{tool} alignment failed: {e}") from e


# Ultima's duplicate marking (Picard MarkDuplicates in flow mode, as in Ultima's
# germline pipeline): single-end reads are duplicates only when both ends
# match, a read trimmed at the adapter (its tm string has an A) having a known end.
ULTIMA_DUPLICATE_ARGS = [
    "--FLOW_MODE", "true",
    "--FLOW_USE_END_IN_UNPAIRED_READS", "true",
    "--FLOW_USE_UNPAIRED_CLIPPED_END", "true",
    "--FLOW_UNPAIRED_END_UNCERTAINTY", "0",
]  # fmt: skip


def ultima_read_groups(header: str, args: argparse.Namespace, stem: str) -> list[str]:
    """The @RG lines of an Ultima BAM/CRAM for the realigned output, which keeps
    each read's RG: the flow order (FO) and max homopolymer class (mc) GATK
    needs live there. Output flow tags are in reference orientation (tp:)."""
    sample = getattr(args, "sample", None)
    lines = []
    for rg in ultima.read_groups(header):
        rg.setdefault("SM", sample or pair_stem(stem))
        if sample:
            rg["SM"] = sample
        rg.setdefault("PL", ultima.PLATFORM)
        rg["tp"] = "reference"
        lines.append("\t".join(["@RG", *(f"{k}:{v}" for k, v in rg.items())]))
    return lines


def align_ultima(
    args: argparse.Namespace, reads: str, cram_opt: list[str] | None = None
) -> None:
    """Align Ultima reads (FASTQ, or a BAM/CRAM: unaligned from the sequencer,
    or aligned, to realign) with BWA-MEM as Ultima's pipeline does. The flow
    tags travel through the FASTQ comment (-C) in each read's own orientation;
    -Y soft-clips supplementary alignments, so every record keeps the whole
    read its tp/t0 arrays describe. Duplicates are marked in flow mode."""
    setup, tool = _bwa_setup(args, "Ultima alignment")
    ext = ".cram" if args.format == "CRAM" else ".bam"
    out_path = str(Path(setup.outdir, f"{setup.stem}_aligned{ext}"))
    logging.info(
        LOG_MESSAGES["aligning_reads"].format(input=reads, output=out_path, tool=tool)
    )
    bwa = [tool, "mem", "-K", BWA_BATCH_BASES, "-t", setup.threads, "-Y"]
    with tempfile.TemporaryDirectory(dir=setup.outdir, prefix=".wgse_ug_") as tmp:
        stages: list[Sequence[object]] = []
        if reads.lower().endswith((".bam", ".cram")):
            header = get_bam_header(reads, cram_opt)
            tagged = True
            rg_lines = ultima_read_groups(header, args, setup.stem)
            tags = [*ultima.READ_TAGS, *(["RG"] if rg_lines else [])]
            stages += [
                ["samtools", "view", "-h", "--no-PG", "-F", "0x900"]
                + [*(cram_opt or []), reads],
                ultima.flow_tag_filter(),
                ["samtools", "fastq", "-n", "-T", ",".join(tags), "-"],
            ]
            if rg_lines:
                rg_file = Path(tmp, "read_groups.sam")
                rg_file.write_text("\n".join(rg_lines) + "\n", newline="\n")
                bwa += ["-H", str(rg_file)]
            else:
                bwa += ["-R", _ultima_read_group(args, setup.stem, tagged)]
            bwa += ["-C", setup.ref, "-"]
        else:
            tagged = ultima.fastq_has_flow_tags(reads)
            bwa += ["-R", _ultima_read_group(args, setup.stem, tagged)]
            bwa += [*(["-C"] if tagged else []), setup.ref, reads]
        if not tagged:
            logging.info(
                f"{reads} has no flow tags (tp, t0): variant callers will treat "
                "its alignment as ordinary short reads."
            )
        stages.append(bwa)
        if tagged:
            stages.append(ultima.flow_tag_filter())
        try:
            _sort_and_mark_ultima(stages, out_path, setup, args.format, tmp)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            raise WGSExtractError(f"{tool} alignment failed: {e}") from e


def _ultima_read_group(args: argparse.Namespace, stem: str, tagged: bool) -> str:
    """read_group's line, plus the flow order GATK needs for tagged reads."""
    rg = read_group(args, stem)
    return rg + f"\\tFO:{ultima.DEFAULT_FLOW_ORDER}\\ttp:reference" if tagged else rg


def _sort_and_mark_ultima(
    stages: list[Sequence[object]],
    out_path: str,
    setup: _AlignSetup,
    fmt: str,
    tmp: str,
) -> None:
    """Sort the aligner's output, mark duplicates with Picard's flow mode (GATK),
    or samtools markdup when GATK is missing, then index."""
    if get_tool_path("gatk") is None:
        logging.warning(
            "GATK isn't installed, so duplicates are marked with samtools markdup, "
            "by where reads start only. Ultima's flow mode also needs their ends "
            "to match; install GATK (see `wgsextract deps`) for it."
        )
        run_streamed_alignment(
            stages[-1],
            out_path,
            threads=setup.threads,
            memory=setup.memory,
            fmt=fmt,
            ref=setup.ref,
            duplicates="markdup",
            upstream=stages[:-1],
        )
        return
    sorted_bam = str(Path(tmp, "sorted.bam"))
    marked = out_path if fmt == "BAM" else str(Path(tmp, "marked.bam"))
    metrics = str(Path(setup.outdir, f"{setup.stem}_duplicate_metrics.txt"))
    sort = get_sam_sort_cmd(
        sorted_bam, setup.threads, setup.memory, sam_input=True, temp_dir=tmp
    )
    partial = [out_path, out_path + ".bai", out_path + ".crai"]
    with cleanup_files(on_failure=partial):
        run_pipeline([*stages, sort], description="Alignment")
        logging.info("Marking duplicates in flow mode (GATK MarkDuplicates)...")
        run_command(
            ["gatk", "MarkDuplicates", "-I", sorted_bam, "-O", marked, "-M", metrics]
            + ["--TMP_DIR", tmp, "--VALIDATION_STRINGENCY", "SILENT"]
            + ULTIMA_DUPLICATE_ARGS
        )
        Path(sorted_bam).unlink(missing_ok=True)
        if fmt == "CRAM":
            run_command(
                ["samtools", "view", "-C", "-@", setup.threads, "-T", setup.ref]
                + ["-o", out_path, marked]
            )
        logging.info(LOG_MESSAGES["indexing_output"])
        run_command(get_sam_index_cmd(out_path, threads=setup.threads))


def _run_gpu(args: argparse.Namespace, aligner: str) -> None:
    """The same alignment on an NVIDIA GPU: Parabricks fq2bam for BWA, its
    minimap2 for HiFi and ONT reads (map-pbmm2 standing in for pbmm2)."""
    if aligner == "bwa":
        _align_gpu_bwa(args)
        return
    if aligner == "pbmm2":
        if (args.preset or ("subread" if args.platform == "clr" else "ccs")) not in (
            "ccs",
            "map-hifi",
        ):
            raise WGSExtractError(
                "--gpu aligns PacBio HiFi/CCS reads, not CLR/subreads."
            )
        _align_gpu_minimap2(args, "map-pbmm2")
        return
    preset = _minimap2_preset(args)
    if preset not in _GPU_MINIMAP2_PRESETS:
        raise WGSExtractError(
            f"--gpu has no minimap2 preset for {preset}: it aligns HiFi (map-hifi) "
            "and ONT (map-ont) long reads, and short reads with --aligner bwa."
        )
    _align_gpu_minimap2(args, _GPU_MINIMAP2_PRESETS[preset])


def _align_gpu_bwa(args: argparse.Namespace) -> None:
    """BWA-MEM, sorting and duplicate marking on the GPU (pbrun fq2bam), with
    the same index, ALT contigs and read group as align_bwa."""
    from wgsextract_cli.core.parabricks import host_path, verify_parabricks

    setup = _prepare_alignment(args, ["samtools"], "GPU alignment")
    verify_parabricks()
    ensure_bwa_index(setup.ref)
    ensure_bwa_alt_file(setup.ref)
    # fq2bam requires a PU (platform unit) in the read group.
    rg = read_group(args, setup.stem) + f"\\tPU:{setup.stem}"
    reads = [host_path(args.r1)] + ([host_path(args.r2)] if args.r2 else [])
    fq_arg = "--in-fq" if args.r2 else "--in-se-fq"
    _gpu_align(args, setup, "fq2bam", [fq_arg, *reads, rg], reads)


def _align_gpu_minimap2(args: argparse.Namespace, preset: str) -> None:
    """Long reads (FASTQ or unaligned BAM) with Parabricks minimap2."""
    from wgsextract_cli.core.parabricks import host_path, verify_parabricks

    if args.r2:
        raise WGSExtractError("Long-read alignment takes single-end input; drop --r2.")
    setup = _prepare_alignment(args, ["samtools"], "GPU long-read alignment")
    verify_parabricks()
    reads = host_path(args.r1)
    in_arg = "--in-bam" if reads.lower().endswith(".bam") else "--in-fq"
    sample = getattr(args, "sample", None) or pair_stem(setup.stem)
    platform = _RG_PLATFORMS.get(getattr(args, "platform", None) or "", "PACBIO")
    _gpu_align(
        args,
        setup,
        "minimap2",
        [in_arg, reads, "--preset", preset]
        + ["--read-group-sm", sample, "--read-group-lb", sample]
        + ["--read-group-pl", platform, "--read-group-id-prefix", setup.stem],
        [reads],
    )
    # From an unaligned BAM, Parabricks keeps the BAM's own read groups, which
    # PacBio writes without the sample name callers need.
    _ensure_read_group_sample(_gpu_output_path(args, setup), sample, setup.ref)


def _gpu_output_path(args: argparse.Namespace, setup: _AlignSetup) -> str:
    from wgsextract_cli.core.parabricks import host_path

    ext = ".cram" if args.format == "CRAM" else ".bam"
    return host_path(str(Path(setup.outdir, f"{setup.stem}_aligned{ext}")))


def _ensure_read_group_sample(path: str, sample: str, ref: str) -> None:
    """Add SM:<sample> to read groups without one (header only, in place)."""
    cram = ["-T", ref] if path.endswith(".cram") else []
    header = run_command(
        ["samtools", "view", "-H", *cram, path], capture_output=True
    ).stdout
    lines = header.splitlines()
    fixed = [
        line + f"\tSM:{sample}"
        if line.startswith("@RG") and "\tSM:" not in line
        else line
        for line in lines
    ]
    if fixed == lines:
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sam", dir=str(Path(path).parent), delete=False
    ) as new_header:
        new_header.write("\n".join(fixed) + "\n")
    try:
        if path.endswith(".cram"):
            run_command(["samtools", "reheader", "-i", new_header.name, path])
        else:
            rewritten = path + ".reheader"
            with open(rewritten, "wb") as out:
                run_command(["samtools", "reheader", new_header.name, path], stdout=out)
            os.replace(rewritten, path)
            run_command(get_sam_index_cmd(path))
    finally:
        Path(new_header.name).unlink(missing_ok=True)
    logging.info(f"Added the sample name {sample} to the read groups of {path}.")


def _gpu_align(
    args: argparse.Namespace,
    setup: _AlignSetup,
    tool: str,
    tool_args: list[str],
    reads: list[str],
) -> None:
    """Run a Parabricks aligner into <stem>_aligned.bam/.cram. Parabricks can't
    write CRAM against a gzipped reference (it crashes), so then it writes BAM
    and samtools converts it."""
    from wgsextract_cli.core.parabricks import host_path, run_pbrun

    out_path = _gpu_output_path(args, setup)
    convert = out_path.endswith(".cram") and setup.ref.lower().endswith(".gz")
    pb_out = out_path.removesuffix(".cram") + ".bam" if convert else out_path
    logging.info(
        LOG_MESSAGES["aligning_reads"].format(
            input=args.r1, output=out_path, tool=f"Parabricks {tool}"
        )
    )
    with (
        cleanup_files(on_failure=[out_path, pb_out, pb_out + ".bai"]),
        tempfile.TemporaryDirectory(dir=setup.outdir, prefix=".wgse_pb_") as tmp,
    ):
        run_pbrun(
            tool,
            ["--ref", host_path(setup.ref), *tool_args]
            + ["--out-bam", pb_out, "--tmp-dir", host_path(tmp)],
            [setup.ref, *reads, pb_out, tmp],
        )
        if convert:
            run_command(
                ["samtools", "view", "-C", "-@", setup.threads, "-T", setup.ref]
                + ["-o", out_path, pb_out]
            )
            run_command(get_sam_index_cmd(out_path, threads=setup.threads))
            for path in (pb_out, pb_out + ".bai"):
                Path(path).unlink(missing_ok=True)
    # Parabricks also lists the output's chromosomes beside it.
    Path(pb_out.rsplit(".", 1)[0] + "_chrs.txt").unlink(missing_ok=True)


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
    align_cmd = ["minimap2", "-ax", preset, "-t", setup.threads]
    align_cmd += ["-R", read_group(args, setup.stem), setup.ref, args.r1]
    try:
        run_streamed_alignment(
            align_cmd + ([args.r2] if args.r2 else []),
            out_bam,
            threads=setup.threads,
            memory=setup.memory,
            fmt=args.format,
            ref=setup.ref,
            duplicates=(
                duplicate_marker(paired=bool(args.r2)) if preset == "sr" else None
            ),
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
