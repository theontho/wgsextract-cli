"""
(Re)align an existing BAM/CRAM to a reference genome.

Workflow:
  1. Resolve FASTQ inputs:
     - If ``--r1`` (and optionally ``--r2``) is provided, use those.
     - Otherwise look next to the input BAM and in the output directory for
       previously extracted FASTQ files (common naming patterns: ``<base>_R1.fastq.gz``, ``<base>.r1.fq.gz``,
       etc). If found, reuse them rather than re-extracting.
     - Otherwise stream paired FASTQ into BWA, or extract when files are needed.
  2. Pre-flight resource check: estimate disk space for both the extraction
     stage (if needed) and the alignment stage, and refuse to start unless
     enough free space is available at the output directory. This avoids the
     classic failure mode of running an alignment for hours only to crash at
     the sort stage because of disk pressure.
  3. Delegate to the existing align command.

Extraction (when FASTQs are written to files) and alignment are steps (see
core.steps): a rerun after a crash or kill skips an alignment or extraction
that finished, and redoes the one that didn't.
"""

import argparse
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api._params import to_namespace
from wgsextract_cli.api.align import RealignParams
from wgsextract_cli.core import bwa_mem2, ultima
from wgsextract_cli.core.alignment_memory import available_memory, check_mapping_memory
from wgsextract_cli.core.alignment_metadata import cram_encoded_with, get_bam_header
from wgsextract_cli.core.atomic_output import atomic_output
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.read_counts import (
    alignment_sample,
    fastq_input_statistics,
    unpaired_sample,
)
from wgsextract_cli.core.reference_assets import (
    find_fasta_by_signature,
    input_stem,
    resolve_reference_library,
)
from wgsextract_cli.core.resource_estimates import (
    PreflightEstimate,
    estimate_resources,
    preflight_resources,
)
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.sequencing import input_platform
from wgsextract_cli.core.steps import (
    Decision,
    Step,
    StepJournal,
    resume_policy,
)
from wgsextract_cli.core.temp_directory import configured_temp_directory
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
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
    indexed_read_count,
)

from . import align as align_cmd
from ._extract_helpers import (
    alignments_to_fastq,
    collate_working_memory,
    paired_fastq_stream,
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


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "realign",
        parents=[base_parser],
        help=CLI_HELP["cmd_realign"],
    )
    add_arguments(parser, RealignParams)
    parser.set_defaults(func=cli_handler(RealignParams, run_realign))


def run_realign(params: RealignParams) -> None:
    """The ``realign`` operation (``api.align.realign``)."""
    align_cmd.add_read_inputs(params.r1, params.r2)
    run(to_namespace(params))


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
    input_path: str,
    outdir: str,
    need_extract: bool,
    reads: int | None,
    keep_fastq: bool = False,
    *,
    streaming: bool = False,
) -> None:
    """Estimate the peak disk usage and bail out if outdir lacks space."""
    file_size = Path(input_path).stat().st_size
    is_cram = input_path.lower().endswith(".cram")

    extract_temp_gb, extract_final_gb = (0, 0)
    if need_extract or streaming:
        extract_temp_gb, extract_final_gb = get_free_space_needed(
            file_size, sort_type="Name", is_cram=is_cram, reads=reads
        )
        if streaming:
            extract_final_gb = 0

    align_temp_gb, align_final_gb = get_free_space_needed(
        file_size, sort_type="Coord", is_cram=is_cram, reads=reads
    )
    scratch = configured_temp_directory()
    if scratch and Path(scratch).stat().st_dev != Path(outdir).stat().st_dev:
        retained_fastq = extract_final_gb if keep_fastq else 0
        staged_fastq = 0 if keep_fastq else extract_final_gb
        requirements = [
            (outdir, retained_fastq + align_final_gb),
            (
                scratch,
                staged_fastq + max(extract_temp_gb, align_temp_gb),
            ),
        ]
        for directory, gigabytes in requirements:
            logging.warning(
                "Realign pre-flight: ~%d GB free space required at %s.",
                gigabytes,
                directory,
            )
            if not check_free_space(directory, gigabytes):
                raise WGSExtractError(
                    f"Insufficient free disk space at {directory} for realign (needs ~{gigabytes} GB)."
                )
        return

    # Collate writes every bucket before it emits a read, then deletes each one as
    # it drains while the sorter's spills grow. Streaming therefore peaks like
    # file extraction without the FASTQs between the two stages.
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
    target = getattr(args, "ref", None)
    # Re-mapping to the same build (e.g. with a newer aligner) decodes with --ref.
    if target and Path(target).is_file() and cram_encoded_with(target, args.input):
        fasta: str | None = target
    else:
        fasta = find_fasta_by_signature(
            [reflib, str(Path(reflib, "genomes"))], signature
        )
    if not fasta:
        raise WGSExtractError(
            f"Neither --ref nor any FASTA in the reference library ({reflib}) "
            "matches the sequence dictionary of the input CRAM. Pass the "
            "reference it was encoded with via --input-ref."
        )
    logging.info("Decoding input CRAM with %s", fasta)
    return ["-T", fasta]


def _refuse_unpaired_reads(path: str, cram_opt: list[str]) -> None:
    """Paired extraction drops single-end reads; refuse rather than lose them."""
    sample = unpaired_sample(path, cram_opt=cram_opt)
    if sample.unpaired:
        raise WGSExtractError(
            f"{sample.unpaired:,} of {sample.records:,} sampled primary reads in "
            f"{path} (e.g. {sample.example}) are single-end: flagged neither first "
            "nor second of a pair. realign extracts paired reads and would drop "
            "them. Extract all reads with 'wgsextract bam unalign --r1 R1.fastq.gz "
            "--r2 R2.fastq.gz --se SE.fastq.gz' and align SE.fastq.gz (and any "
            "pairs) with 'wgsextract align', or pass --platform for ONT, PacBio "
            "or Ultima reads."
        )


def _single_end_pacbio(args: argparse.Namespace, cram_opt: list[str]) -> str | None:
    """ "pacbio" (or "clr" for subreads) when every sampled primary read of a
    PacBio input is single-end, as PacBio reads are: realign then takes the
    long-read path instead of refusing them. Paired PacBio-labelled input keeps
    the short-read defaults."""
    sample = unpaired_sample(args.input, cram_opt=cram_opt)
    if not sample.records or sample.unpaired != sample.records:
        return None
    platform = align_cmd.pacbio_platform(
        get_bam_header(args.input, cram_opt[1] if cram_opt else None)
    )
    logging.info(
        "Detected single-end PacBio reads: realigning them as long reads "
        "(--platform %s).",
        platform,
    )
    return platform


def run(args: argparse.Namespace) -> None:
    if not args.input:
        raise UsageError(
            "--input <BAM|CRAM> is required for realign. Pass it before the "
            "subcommand or via --input."
        )
    verify_paths_exist({"--input": args.input})
    platform_args = args
    input_cram_opt = None
    if not args.platform and not args.r1 and args.input.lower().endswith(".cram"):
        header = get_bam_header(args.input, getattr(args, "input_ref", None))
        if not any(group.get("PL") for group in ultima.read_groups(header)):
            input_cram_opt = _input_cram_reference(args)
            platform_args = argparse.Namespace(**vars(args))
            platform_args.input_ref = input_cram_opt[1]
    detected = None
    if not args.platform and not args.r1:
        input_ref = getattr(platform_args, "input_ref", None)
        if input_platform(args.input, input_ref, strict=True) == "pacbio":
            detected = _single_end_pacbio(
                args, input_cram_opt or _input_cram_reference(platform_args)
            )
    args.platform = detected or align_cmd.resolve_platform(
        platform_args, args.r1 or args.input
    )
    if align_cmd.is_long_read(args) and not args.r1:
        _check_long_read_options(args)
    # Reject bad aligner/platform/preset combinations before hours of extraction.
    selected_aligner = align_cmd.select_aligner(args)
    if (
        selected_aligner == "pbmm2"
        and configured_temp_directory()
        and not getattr(args, "gpu", False)
    ):
        raise UsageError(
            "Native pbmm2's internal sorter does not support --tmp-dir. Use --aligner minimap2 or omit the temporary-directory setting."
        )
    signature = None
    if selected_aligner in {"bwa", "bwa-mem2"} and not (
        args.ref and Path(args.ref).is_file()
    ):
        signature = calculate_bam_md5(args.input, None)
    target = resolve_reference(args.ref, signature) if args.ref or signature else None
    if args.aligner == "bwa-mem2" and target and Path(target).is_file():
        align_cmd.ensure_fasta_index(target)
        bwa_mem2.check_index(target)

    threads, memory = get_resource_defaults(args.threads, args.memory)
    outdir = args.outdir or str(absolute_path(args.input).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)

    policy = resume_policy(args)
    if args.r1:
        logical_r1, logical_r2 = args.r1, args.r2
    elif args.platform == "ultima" or align_cmd.is_long_read(args):
        logical_r1, logical_r2 = args.input, None
    else:
        base = input_stem(args.input)
        logical_r1 = f"{base}_R1.fastq.gz"
        logical_r2 = f"{base}_R2.fastq.gz"
    journal_output = align_cmd.aligned_output_path(
        outdir, logical_r1, logical_r2, getattr(args, "format", "BAM"), selected_aligner
    )
    with StepJournal.for_output(journal_output, "realign", policy) as journal:
        _run_steps(
            args,
            journal,
            selected_aligner=selected_aligner,
            target=target,
            outdir=outdir,
            threads=threads,
            memory=memory,
            input_cram_opt=input_cram_opt,
        )


def _check_long_read_options(args: argparse.Namespace) -> None:
    """realign streams long reads from the BAM/CRAM into CPU minimap2."""
    if args.aligner == "pbmm2":
        if args.platform not in align_cmd.PACBIO_PLATFORMS:
            raise UsageError(
                "pbmm2 aligns PacBio reads only; realign maps ONT reads with "
                "minimap2 (--aligner minimap2 or auto)."
            )
        raise UsageError(
            "realign aligns PacBio reads with minimap2, streaming each read's "
            "primary record with its MM/ML tags. pbmm2 reads a PacBio BAM "
            "itself, primary records only: run 'wgsextract align --r1 "
            "<BAM> --aligner pbmm2' (convert a CRAM with 'wgsextract bam "
            "to-bam' first)."
        )
    if getattr(args, "gpu", False):
        raise UsageError(
            "realign aligns long reads on the CPU with minimap2, keeping their "
            "MM/ML tags; drop --gpu."
        )
    if args.aligner == "auto":
        args.aligner = "minimap2"


def _align_tools(aligner: str, platform: str | None, gpu: bool) -> list[str]:
    """The tools whose versions shape an alignment (bwa may become bwa-mem2)."""
    if gpu:
        return ["samtools", "pbrun"]
    tools = {
        "bwa": ["bwa", "bwa-mem2", "samblaster"],
        "bwa-mem2": ["bwa-mem2", "samblaster"],
        "minimap2": ["minimap2"],
        "pbmm2": ["pbmm2"],
    }.get(aligner, [aligner])
    return ["samtools", *tools, *(["gatk"] if platform == "ultima" else [])]


def _align_step(
    args: argparse.Namespace,
    aligner: str,
    target: str | None,
    out_path: str,
    reads: dict[str, str | None],
) -> Step:
    """The alignment step: map, mark duplicates and sort into out_path.
    Its reads are the input BAM/CRAM, also when they reach the aligner through
    extracted FASTQs (_run_steps reruns it whenever extraction reruns)."""
    reference = target if target and Path(target).is_file() else None
    return Step(
        "align",
        inputs={
            **reads,
            "reference": reference,
            "input_reference": getattr(args, "input_ref", None),
        },
        outputs=[out_path],
        params={
            "aligner": aligner,
            "platform": args.platform,
            "preset": args.preset,
            "long_read": bool(args.long_read),
            "format": getattr(args, "format", "BAM"),
            "sample": getattr(args, "sample", None),
            "gpu": bool(getattr(args, "gpu", False)),
            "ref": None if reference else args.ref,
        },
        tools=_align_tools(aligner, args.platform, bool(getattr(args, "gpu", False))),
    )


def _index_path(out_path: str) -> str:
    return out_path + (".crai" if out_path.endswith(".cram") else ".bai")


def _index_step(out_path: str) -> Step:
    return Step(
        "index",
        inputs={"alignment": out_path},
        outputs=[_index_path(out_path)],
        tools=["samtools"],
    )


def _align(
    journal: StepJournal,
    decision: Decision,
    align_args: argparse.Namespace,
    run: Callable[[], None],
) -> None:
    """Run the align step. Aligners that report when their sorted output is
    complete (bwa, minimap2) index it as a separate step, so a run killed
    while indexing resumes without realigning. The others index within it,
    and that index is then recorded as the index step."""
    out_path = str(decision.step.outputs[0])
    aligning = journal.start(decision)
    indexing = None

    def written(tool: str) -> None:
        nonlocal indexing
        aligning.metrics["aligner"] = tool
        journal.finish(aligning)
        indexing = journal.start(journal.decide(_index_step(out_path)))

    align_args._on_written = written
    run()
    if indexing is not None:
        journal.finish(indexing)
    else:
        aligning.metrics["aligner"] = current_result().metrics.get("aligner")
        journal.finish(aligning)
        # The aligner indexed its output itself; record that as the index step.
        index = journal.check(_index_step(out_path))
        journal.finish(journal.start(index, keep_partial=True))


def _resume_alignment(
    journal: StepJournal,
    decision: Decision,
    out_path: str,
    target: str | None,
    threads: str,
) -> None:
    """Finish from a completed align step: index the output unless the index
    step is complete, then report the alignment as this run's output."""
    indexing = journal.decide(_index_step(out_path))
    if journal.dry_run:
        return
    if indexing.run:
        with journal.running(indexing):
            logging.info("Indexing %s ...", out_path)
            align_cmd._index_output(out_path, threads=threads)
    aligner = str(decision.metrics.get("aligner") or "aligner")
    align_cmd.record_alignment(out_path, aligner, target or "")


def _realign_long_reads(
    args: argparse.Namespace,
    journal: StepJournal,
    *,
    selected_aligner: str,
    target: str | None,
    outdir: str,
    threads: str,
    input_cram_opt: list[str] | None,
) -> None:
    """ONT and PacBio reads are single-end and carry base-modification tags:
    each read's primary record, tags included, streams into minimap2. With
    --keep-fastq they are extracted to a tagged <input stem>.fastq.gz first."""
    out_path = align_cmd.aligned_output_path(
        outdir, args.input, None, getattr(args, "format", "BAM"), selected_aligner
    )
    # The aligner names its output after the FASTQ, so it takes the stem that
    # names out_path (movie.ccs.bam -> movie.fastq.gz -> movie_aligned.bam).
    fastq = str(
        absolute_path(Path(outdir, f"{align_cmd.reads_stem(args.input)}.fastq.gz"))
    )
    extract_step = Step(
        "extract",
        inputs={
            "alignment": args.input,
            "input_reference": getattr(args, "input_ref", None),
        },
        outputs=[fastq],
        params={"tags": align_cmd.LONG_READ_TAGS},
        tools=["samtools", "bgzip"],
    )
    align_step = _align_step(
        args, selected_aligner, target, out_path, {"alignment": args.input}
    )
    if args.keep_fastq and args.force_extract:
        journal.policy = replace(
            journal.policy, force_steps=journal.policy.force_steps | {"extract"}
        )
    if not journal.policy.forced("extract") and not journal.check(align_step).run:
        # An earlier run finished: nothing before the alignment is needed.
        _resume_alignment(
            journal, journal.decide(align_step), out_path, target, threads
        )
        if args.keep_fastq and not journal.dry_run:
            if Path(fastq).is_file():
                current_result().add_output(fastq, "fastq_r1")
            else:
                logging.info(
                    "The alignment is complete, so no FASTQ was extracted; "
                    "--force-extract writes one and realigns."
                )
        return
    extract_decision = journal.decide(extract_step) if args.keep_fastq else None
    decision = journal.decide(
        align_step, after=[extract_decision] if extract_decision else []
    )
    if journal.dry_run:
        journal.decide(_index_step(out_path))
        return
    cram_opt = input_cram_opt or _input_cram_reference(args)
    if not args.no_preflight:
        _preflight_disk_space(
            args.input,
            outdir,
            need_extract=bool(extract_decision and extract_decision.run),
            reads=indexed_read_count(args.input, threads),
            keep_fastq=args.keep_fastq,
        )
    align_args = argparse.Namespace(**vars(args))
    align_args.r1 = args.input
    # samtools fastq's -T lists tags; the reference needs --reference.
    decode = ["--reference", cram_opt[1]] if cram_opt else []
    align_args._input_cram_opt = decode
    if extract_decision is not None:
        if extract_decision.run:
            verify_dependencies(["samtools", "bgzip"])
            logging.info("Extracting tagged single-end FASTQ from %s ...", args.input)
            with (
                journal.running(extract_decision),
                atomic_output(fastq) as staged,
                open(staged, "wb") as handle,
            ):
                run_pipeline(
                    [
                        align_cmd.long_read_fastq_command(args.input, decode),
                        ["bgzip", "-@", threads, "-c"],
                    ],
                    stdout=handle,
                    description="FASTQ extraction",
                    heartbeat=True,
                )
        align_args.r1 = fastq
        align_args._read_group_description = align_cmd.read_group_descriptions(
            args.input, getattr(args, "input_ref", None)
        )
    _align(journal, decision, align_args, lambda: align_cmd.run(align_args))
    if args.keep_fastq:
        current_result().add_output(fastq, "fastq_r1")


def _run_steps(
    args: argparse.Namespace,
    journal: StepJournal,
    *,
    selected_aligner: str,
    target: str | None,
    outdir: str,
    threads: str,
    memory: str,
    input_cram_opt: list[str] | None,
) -> None:
    """Realign as steps: extract FASTQs (when they're written to files), then
    align. A completed alignment skips everything before it."""
    if align_cmd.is_long_read(args) and not args.r1:
        _realign_long_reads(
            args,
            journal,
            selected_aligner=selected_aligner,
            target=target,
            outdir=outdir,
            threads=threads,
            input_cram_opt=input_cram_opt,
        )
        return

    if args.platform == "ultima" and not args.r1:
        out_path = align_cmd.aligned_output_path(
            outdir, args.input, None, getattr(args, "format", "BAM"), selected_aligner
        )
        step = _align_step(
            args, selected_aligner, target, out_path, {"alignment": args.input}
        )
        decision = journal.decide(step)
        if not decision.run:
            _resume_alignment(journal, decision, out_path, target, threads)
            return
        if journal.dry_run:
            journal.decide(_index_step(out_path))
            return
        # Single-end reads with flow tags: streamed into the aligner, no FASTQs.
        reads = indexed_read_count(args.input, threads)
        _report_resource_estimates(
            args,
            target,
            outdir,
            threads,
            memory,
            reads,
            input_paths=[args.input],
            need_extract=False,
            paired=False,
        )
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
        cram_reference = _input_cram_reference(args)
        _align(
            journal,
            decision,
            align_args,
            lambda: align_cmd.align_ultima(align_args, args.input, cram_reference),
        )
        return

    # 1. Resolve FASTQs: the user's, an earlier run's extraction, a pair found
    # beside the input, or a new extraction (or stream).
    base = input_stem(args.input)
    staging = outdir if args.keep_fastq else configured_temp_directory() or outdir
    staged = (
        str(absolute_path(Path(staging, f"{base}_R1.fastq.gz"))),
        str(absolute_path(Path(staging, f"{base}_R2.fastq.gz"))),
    )
    extract_step = Step(
        "extract",
        inputs={
            "alignment": args.input,
            "input_reference": getattr(args, "input_ref", None),
        },
        outputs=staged,
        tools=["samtools"],
    )
    reads_inputs: dict[str, str | None] = (
        {"fastq_r1": args.r1, "fastq_r2": args.r2}
        if args.r1
        else {"alignment": args.input}
    )
    out_path = align_cmd.aligned_output_path(
        outdir,
        args.r1 or staged[0],
        args.r2 if args.r1 else staged[1],
        getattr(args, "format", "BAM"),
        selected_aligner,
    )
    align_step = _align_step(args, selected_aligner, target, out_path, reads_inputs)
    if (
        not args.r1
        and not args.force_extract
        and not journal.policy.forced("extract")
        and not journal.check(align_step).run
    ):
        # An earlier run finished: nothing before the alignment is needed.
        _resume_alignment(
            journal, journal.decide(align_step), out_path, target, threads
        )
        return

    extract_decision: Decision | None = None
    if args.r1:
        verify_paths_exist({"--r1": args.r1})
        if args.r2:
            verify_paths_exist({"--r2": args.r2})
        r1_path, r2_path = args.r1, args.r2
        need_extract = False
    elif not args.force_extract and not journal.check(extract_step).run:
        extract_decision = journal.decide(extract_step)
        r1_path, r2_path = staged
        need_extract = False
    elif (
        not args.force_extract
        and not journal.policy.forced("extract")
        and (existing := _find_existing_fastqs(args.input, outdir))
        # This run's own extraction, when its record doesn't vouch for it.
        and not (existing == staged and journal.recorded("extract"))
    ):
        r1_path, r2_path = existing
        need_extract = False
        logging.info("Reusing existing FASTQs:\n  R1: %s\n  R2: %s", r1_path, r2_path)
        current_result().add_input(r1_path, "fastq_r1")
        current_result().add_input(r2_path, "fastq_r2")
        out_path = align_cmd.aligned_output_path(
            outdir, r1_path, r2_path, getattr(args, "format", "BAM"), selected_aligner
        )
        align_step = _align_step(
            args,
            selected_aligner,
            target,
            out_path,
            {"alignment": args.input, "fastq_r1": r1_path, "fastq_r2": r2_path},
        )
    else:
        r1_path, r2_path = staged
        need_extract = True

    refresh_cached_fastqs = (
        need_extract
        and args.force_extract
        and any(Path(path).is_file() for path in (r1_path, r2_path) if path)
    )
    streaming = (
        need_extract
        and selected_aligner in {"bwa", "bwa-mem2"}
        and not getattr(args, "keep_fastq", False)
        and not getattr(args, "gpu", False)
        and not refresh_cached_fastqs
    )
    align_decision: Decision | None = None
    if journal.dry_run or not need_extract:
        if need_extract and not streaming:
            extract_decision = journal.decide(extract_step)
        align_decision = journal.decide(
            align_step, after=[extract_decision] if extract_decision else []
        )
        if not align_decision.run:
            _resume_alignment(journal, align_decision, out_path, target, threads)
            return
        if journal.dry_run:
            journal.decide(_index_step(out_path))
            return
    statistics = None if need_extract else fastq_input_statistics(r1_path, r2_path)
    reads = (
        indexed_read_count(args.input, threads)
        if need_extract
        else statistics.reads
        if statistics is not None
        else None
    )
    read_length = statistics.read_length if statistics is not None else None
    additional_ram_bytes = 0
    cram_opt: list[str] = []
    if need_extract:
        verify_dependencies(["samtools"])
        cram_opt = (
            input_cram_opt
            if input_cram_opt is not None
            else _input_cram_reference(args)
        )
        _refuse_unpaired_reads(args.input, cram_opt)
    if streaming:
        sample = alignment_sample(args.input, cram_opt=cram_opt)
        read_length = sample.read_length
        # One collate bucket is resident while BWA maps; reserve it alongside.
        additional_ram_bytes = collate_working_memory(
            args.input,
            threads,
            memory,
            records=reads,
            max_record_bytes=sample.max_record_bytes,
        )
        fits = additional_ram_bytes <= available_memory()
        if fits and target and Path(target).is_file():
            samblaster = (
                align_cmd.duplicate_marker(paired=bool(r2_path)) == "samblaster"
            )
            tool = align_cmd.short_read_aligner(
                args.aligner,
                target,
                threads=threads,
                memory=memory,
                samblaster=samblaster,
                reads=reads,
            )
            fits = align_cmd.streaming_mapping_fits(
                target,
                tool,
                threads=threads,
                memory=memory,
                samblaster=samblaster,
                reads=reads,
                platform=args.platform,
                read_length=read_length,
                additional_ram_bytes=additional_ram_bytes,
            )
        if not fits:
            logging.info(
                "Holding a collate bucket (~%.1f GiB) beside alignment does not "
                "fit available RAM; extracting FASTQs without changing the "
                "aligner.",
                additional_ram_bytes / 1024**3,
            )
            streaming = False
            additional_ram_bytes = 0
    if selected_aligner in {"bwa", "bwa-mem2"} and not getattr(args, "gpu", False):
        _report_resource_estimates(
            args,
            target,
            outdir,
            threads,
            memory,
            reads,
            input_paths=[args.input] if need_extract else [r1_path, r2_path],
            need_extract=need_extract and not streaming,
            paired=bool(r2_path),
            read_length=read_length,
            additional_ram_bytes=additional_ram_bytes,
        )
    if args.aligner == "bwa-mem2" and target and Path(target).is_file():
        check_mapping_memory(
            target,
            "bwa-mem2",
            threads=threads,
            memory=align_cmd.short_read_sort_memory("bwa-mem2", memory, log=False),
            samblaster=align_cmd.duplicate_marker(paired=bool(r2_path)) == "samblaster",
            reads=reads,
            read_length=read_length,
            platform=args.platform,
            additional_ram_bytes=0,
        )

    # 2. Disk-space pre-flight.
    if not args.no_preflight:
        try:
            _preflight_disk_space(
                args.input,
                outdir,
                need_extract=need_extract and not streaming,
                reads=reads,
                keep_fastq=args.keep_fastq,
                streaming=streaming,
            )
        except WGSExtractError:
            if not streaming:
                raise
            logging.info(
                "Streaming spills do not fit available disk; extracting "
                "FASTQs without changing the aligner."
            )
            streaming = False
            additional_ram_bytes = 0
            _report_resource_estimates(
                args,
                target,
                outdir,
                threads,
                memory,
                reads,
                input_paths=[args.input],
                need_extract=True,
                paired=bool(r2_path),
                read_length=read_length,
            )
            _preflight_disk_space(
                args.input,
                outdir,
                need_extract=True,
                reads=reads,
                keep_fastq=args.keep_fastq,
            )

    if streaming:
        logging.info(
            "Streaming paired FASTQ from %s into BWA; no gzipped FASTQ files.",
            args.input,
        )
        align_args = argparse.Namespace(**vars(args))
        # Logical mate names preserve existing output stems/read groups, not read paths.
        align_args.r1, align_args.r2 = r1_path, r2_path
        align_args._input_read_count = reads
        align_args._input_read_length = read_length
        align_decision = journal.decide(align_step)
        try:
            with paired_fastq_stream(
                args.input,
                threads=threads,
                memory=memory,
                temp_parent=outdir,
                cram_opt=cram_opt,
            ) as stages:
                _align(
                    journal,
                    align_decision,
                    align_args,
                    lambda: align_cmd.align_bwa(
                        align_args,
                        upstream=stages,
                        read_paths=[args.input],
                        additional_ram_bytes=additional_ram_bytes,
                    ),
                )
        except align_cmd.StreamingMemoryUnavailable:
            logging.info(
                "Available RAM changed before streaming started; extracting "
                "FASTQs without changing the aligner."
            )
            _report_resource_estimates(
                args,
                target,
                outdir,
                threads,
                memory,
                reads,
                input_paths=[args.input],
                need_extract=True,
                paired=bool(r2_path),
                read_length=read_length,
            )
            if not args.no_preflight:
                _preflight_disk_space(
                    args.input,
                    outdir,
                    need_extract=True,
                    reads=reads,
                    keep_fastq=args.keep_fastq,
                )
        else:
            return

    # 3. Extract if needed, publishing each FASTQ only once it is complete.
    if need_extract:
        verify_dependencies(["samtools"])
        log_dependency_info(["samtools"])
        extract_decision = journal.decide(extract_step)
        logging.info("Extracting FASTQs from %s ...", args.input)
        with (
            journal.running(extract_decision) as extracting,
            atomic_output(r1_path) as staged_r1,
            atomic_output(r2_path) as staged_r2,
        ):
            extracting.metrics["temporary"] = not args.keep_fastq
            alignments_to_fastq(
                args.input,
                staged_r1,
                staged_r2,
                threads=threads,
                memory=memory,
                temp_parent=outdir,
                cram_opt=cram_opt,
                heartbeat=True,
                expected_reads=reads,
            )

    # 4. Delegate to align.
    align_args = argparse.Namespace(**vars(args))
    align_args.r1 = r1_path
    align_args.r2 = r2_path
    align_args._input_read_count = reads
    if need_extract:
        statistics = fastq_input_statistics(r1_path, r2_path)
        read_length = statistics.read_length if statistics is not None else None
    align_args._input_read_length = read_length
    if align_decision is None:
        align_decision = journal.decide(
            align_step, after=[extract_decision] if extract_decision else []
        )
    try:
        _align(journal, align_decision, align_args, lambda: align_cmd.run(align_args))
    except BaseException:
        if need_extract and not args.keep_fastq:
            from wgsextract_cli.core.temp_directory import retain_temporary_directory

            directory = retain_temporary_directory()
            if directory:
                logging.warning(
                    "Alignment failed; extracted FASTQs are retained in %s. "
                    "Rerun with --r1 and --r2 pointing to:\n  %s\n  %s\n"
                    "Remove the retained workspace after recovery.",
                    directory,
                    r1_path,
                    r2_path,
                )
        raise

    # 5. Remove the FASTQs this workflow extracted unless asked to keep them
    # (now or by the run that extracted them). They are kept when alignment
    # fails so a rerun can reuse them.
    if extract_decision is None:
        return
    temporary = extract_decision.run or extract_decision.metrics.get("temporary")
    if temporary and not args.keep_fastq:
        for path in (r1_path, r2_path):
            try:
                Path(path).unlink()
            except OSError as exc:
                logging.warning("Could not remove %s: %s", path, exc)
        journal.retire("extract", [r1_path, r2_path])
    elif args.keep_fastq:
        current_result().add_output(r1_path, "fastq_r1")
        current_result().add_output(r2_path, "fastq_r2")
    else:  # kept by the --keep-fastq run that extracted them
        current_result().add_input(r1_path, "fastq_r1")
        current_result().add_input(r2_path, "fastq_r2")


def _report_resource_estimates(
    args: argparse.Namespace,
    reference: str | None,
    outdir: str,
    threads: str,
    memory: str,
    reads: int | None,
    *,
    input_paths: list[str],
    need_extract: bool,
    paired: bool,
    read_length: float | None = None,
    additional_ram_bytes: int = 0,
) -> None:
    def estimate() -> PreflightEstimate:
        samblaster = align_cmd.duplicate_marker(paired=paired) == "samblaster"
        resolved = reference if reference and Path(reference).is_file() else None
        tool = "bwa" if args.aligner == "auto" else args.aligner
        if resolved:
            tool = align_cmd.short_read_aligner(
                args.aligner,
                resolved,
                threads=threads,
                memory=memory,
                samblaster=samblaster,
                reads=reads,
            )
        return estimate_resources(
            "realign",
            input_paths=[path for path in input_paths if path],
            output_dir=outdir,
            temp_dir=configured_temp_directory(),
            reference=resolved,
            threads=threads,
            memory=align_cmd.short_read_sort_memory(tool, memory, log=False),
            aligner=tool,
            samblaster=samblaster,
            reads=reads,
            platform=getattr(args, "platform", None),
            read_length=read_length,
            additional_ram_bytes=additional_ram_bytes,
            output_format=getattr(args, "format", "BAM"),
            extract_fastq=need_extract,
            fastq_dir=outdir if args.keep_fastq else configured_temp_directory(),
            duplicate_marking=(
                "gatk-flow"
                if getattr(args, "platform", None) == "ultima"
                and align_cmd.get_tool_path("gatk") is not None
                else None
            ),
        )

    preflight_resources(estimate)
