import contextlib
import logging
import math
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from os import devnull, fspath
from pathlib import Path
from typing import NamedTuple

from wgsextract_cli.core.atomic_output import atomic_outputs_enabled
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import PathInput, absolute_path
from wgsextract_cli.core.reference_assets import input_stem, read_fai_lengths
from wgsextract_cli.core.reference_models import sequence_lengths
from wgsextract_cli.core.run_log import is_run_log_path
from wgsextract_cli.core.samtools_commands import parse_memory
from wgsextract_cli.core.temp_directory import configured_temp_directory
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    resolve_reference,
    verify_paths_exist,
)


class BaseArgs(NamedTuple):
    threads: str
    memory: str
    outdir: str
    cram_opt: list[str]
    ref: str | None


def base_args(
    input_path: PathInput | None,
    *,
    ref: PathInput | None,
    outdir: PathInput | None,
    threads: int | str | None,
    memory: str | None,
    region: str | None = None,
) -> BaseArgs:
    """Validate the input, create the output directory and resolve the reference."""
    if not input_path:
        raise UsageError(LOG_MESSAGES["input_required"])
    input_path = fspath(input_path)
    verify_paths_exist({"--input": input_path})
    logging.debug(f"Input file: {str(absolute_path(input_path))}")

    threads, memory = get_resource_defaults(threads, memory)
    outdir = fspath(outdir) if outdir else str(absolute_path(input_path).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    ref_arg = fspath(ref) if ref else None
    try:
        resolved_ref = resolve_reference(ref_arg, calculate_bam_md5(input_path, None))
    except (OSError, RuntimeError, ValueError) as e:
        raise WGSExtractError(f"Failed to resolve the reference: {e}") from e
    if resolved_ref and Path(resolved_ref).is_dir():
        logging.warning(
            f"No reference matching {input_path} was found in {resolved_ref}; "
            "continuing without one."
        )
        resolved_ref = None
    logging.debug(f"Resolved reference: {resolved_ref}")
    if resolved_ref:
        verify_paths_exist({"--ref": resolved_ref})

    contigs = _alignment_contigs(input_path)
    if contigs:
        resolved_ref = _matching_reference(input_path, ref_arg, resolved_ref, contigs)
        _check_region(region, contigs, input_path)

    cram_opt = ["-T", resolved_ref] if resolved_ref else []
    return BaseArgs(threads, memory, outdir, cram_opt, resolved_ref)


def _alignment_contigs(path: str) -> dict[str, int]:
    """A BAM/CRAM's @SQ lengths (empty for other inputs or an unreadable one)."""
    if not str(path).lower().endswith((".bam", ".cram")):
        return {}
    try:
        header = run_command(["samtools", "view", "-H", path], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return {}
    return sequence_lengths(header.stdout)


def _matching_reference(
    input_path: str, ref_arg: str | None, ref: str | None, contigs: dict[str, int]
) -> str | None:
    """The reference, unless its contigs contradict the alignment's: CRAM
    encoded against the wrong genome only fails when it's decoded."""
    lengths = read_fai_lengths(ref) if ref else {}
    clash = next(
        (n for n, ln in contigs.items() if n in lengths and lengths[n] != ln), None
    )
    if not ref or not clash:
        return ref
    detail = (
        f"{ref} doesn't match {input_path}: {clash} is {lengths[clash]:,} bp there "
        f"and {contigs[clash]:,} bp in the alignment."
    )
    if ref_arg and Path(ref_arg).is_file():
        raise WGSExtractError(detail)
    logging.warning(f"{detail} Continuing without a reference; pass --ref.")
    return None


def _check_region(region: str | None, contigs: dict[str, int], path: str) -> None:
    """samtools writes an empty file for a region naming no contig of the input."""
    if region and Path(region).is_file():  # a BED file of regions
        return
    for part in (r.strip() for r in (region or "").split(",") if r.strip()):
        # A whole name first: HLA allele contigs contain colons.
        contig = part if part in contigs else part.rsplit(":", 1)[0]
        if contig in contigs:
            continue
        bare = contig.removeprefix("chr")
        candidates = {bare, f"chr{bare}"} | (
            {"MT", "chrM", "M"} if bare in ("M", "MT") else set()
        )
        alt = next((c for c in sorted(candidates) if c in contigs), None)
        hint = f"; it names it {alt}" if alt else ""
        raise WGSExtractError(f"{contig} isn't a contig of {path}{hint}.")


def derive_output_path(input_path: PathInput, outdir: PathInput, suffix: str) -> str:
    """Return <outdir>/<input stem><suffix>, refusing to overwrite the input."""
    out_path = Path(outdir) / (input_stem(input_path) + suffix)
    if out_path.resolve() == Path(input_path).resolve() or (
        out_path.exists() and out_path.samefile(input_path)
    ):
        raise WGSExtractError(
            f"Output {out_path} would overwrite the input file; "
            "choose a different --outdir."
        )
    return str(out_path)


@contextlib.contextmanager
def cleanup_files(
    temp: Sequence[PathInput] = (),
    on_failure: Sequence[PathInput] = (),
    *,
    preserve_outputs: bool = False,
) -> Iterator[None]:
    """Always delete ``temp`` files; delete ``on_failure`` (partial outputs) only
    if the block raises, so they are never mistaken for complete results.
    Only staged writers may opt into preserving previous final outputs."""
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        failed = [] if succeeded or preserve_outputs else on_failure
        for path in [*temp, *failed]:
            if is_run_log_path(Path(path)):
                continue
            with contextlib.suppress(FileNotFoundError):
                Path(path).unlink()


def region_or_gene(
    region: str | None,
    gene: str | None,
    input_path: PathInput,
    resolved_ref: str | None,
) -> str | None:
    """Return ``region``, or ``gene`` resolved to coordinates; None if neither is set."""
    if region:
        return str(region)
    if not gene:
        return None

    from wgsextract_cli.core.config import settings
    from wgsextract_cli.core.gene_map import (
        gene_coords,
        gene_map_build,
        resolve_gene_map_reflib,
    )
    from wgsextract_cli.core.reference_resolver import ReferenceLibrary

    detected = ReferenceLibrary(
        resolved_ref, skip_full_search=True, input_path=fspath(input_path)
    ).build
    build = gene_map_build(detected)

    configured_reflib = settings.get("reference_library")
    reflib_dir = resolve_gene_map_reflib(
        resolved_ref,
        str(configured_reflib) if configured_reflib else None,
        build,
    )
    if not reflib_dir:
        raise WGSExtractError(
            "Reference library not found. Please provide a --ref or set "
            "reference_library in config.toml."
        )

    resolved_region = gene_coords(reflib_dir, gene, build)
    logging.info(f"Resolved gene {gene} to {resolved_region}")
    return str(resolved_region)


_COLLATE_HEADROOM = 64 * 1024**2
# Streaming estimates when no read count or record sample is available: the
# worst tiny record-allocation projections (13.42x BAM / 64.83x CRAM) plus 50%,
# rounded up. These are heuristics, not enforced RSS bounds.
_STREAM_BAM_EXPANSION = 30
_STREAM_CRAM_EXPANSION = 100
# samtools collate keeps one bam1_t slot per record of the largest bucket. A
# slot's data buffer only grows, to a power of two, as buckets reuse it; this is
# its fixed bam1_t, sort-element and allocator overhead (measured, see
# resource_estimates.md).
_COLLATE_RECORD_OVERHEAD = 128


def _collate_buckets(input_path: PathInput, threads: str, memory: str) -> int:
    """Temp files for `samtools collate`, which holds one in memory at a time.

    70x CRAM / 20x BAM file size against the sort memory budget (threads x
    per-thread memory). Collate's output order depends on this count, so
    streamed and file extraction use the same one and give identical pairs to
    the aligner. At most 200 files stay under macOS's default limit of 256
    open files.
    """
    try:
        size = Path(input_path).stat().st_size
    except OSError:
        size = 0
    expansion = 70 if Path(input_path).suffix.lower() == ".cram" else 20
    amount, unit = parse_memory(memory)
    budget = int(int(threads) * amount * (1024**3 if unit == "G" else 1024**2))
    return min(200, max(64, math.ceil(size * expansion / budget)))


def collate_working_memory(
    input_path: PathInput,
    threads: str,
    memory: str,
    *,
    records: int | None = None,
    max_record_bytes: int | None = None,
) -> int:
    """Estimated resident memory of `samtools collate` holding its largest bucket.

    With the input's record count and the largest sampled BAM record, each
    held record costs that record's power-of-two buffer plus fixed overhead;
    the largest bucket allows six standard deviations over the mean for mates
    that share a name hash. Otherwise falls back to calibrated 30x BAM / 100x
    CRAM file-size expansions.
    """
    buckets = _collate_buckets(input_path, threads, memory)
    if records and max_record_bytes:
        mean = records / buckets
        held = math.ceil(mean + 6 * math.sqrt(2 * mean))
        slot = 1 << (max_record_bytes - 1).bit_length()
        return held * (slot + _COLLATE_RECORD_OVERHEAD) + _COLLATE_HEADROOM
    try:
        size = Path(input_path).stat().st_size
    except OSError:
        size = 0
    expansion = (
        _STREAM_CRAM_EXPANSION
        if Path(input_path).suffix.lower() == ".cram"
        else _STREAM_BAM_EXPANSION
    )
    return math.ceil(size * expansion / buckets) + _COLLATE_HEADROOM


def _fastq_stages(
    input_path: PathInput,
    out_r1: PathInput,
    out_r2: PathInput,
    *,
    threads: str,
    memory: str,
    temporary: str,
    singletons: PathInput,
    cram_opt: list[str] | None = None,
    region: str | None = None,
    out_other: PathInput | None = None,
) -> list[list[str]]:
    view = ["samtools", "view", "-uh", "--no-PG", "-@", threads]
    view += cram_opt or []
    view += [fspath(input_path), *([region] if region else [])]
    collate = ["samtools", "collate", "-u", "-O", "--no-PG", "-@", threads]
    collate += [
        "-n",
        str(_collate_buckets(input_path, threads, memory)),
        "-T",
        str(Path(temporary, "collate")),
        "-",
    ]
    fastq = ["samtools", "fastq", "-1", fspath(out_r1), "-2", fspath(out_r2)]
    fastq += ["-s", fspath(singletons)]
    if out_other:
        fastq += ["-0", fspath(out_other)]
    fastq += ["-n", "-@", threads, "-"]
    return [view, collate, fastq]


def _warn_unpaired(other: Path) -> None:
    """Warn about READ_OTHER records ``samtools fastq`` wrote to ``other``."""
    import gzip

    if not other.exists():
        return
    with gzip.open(other, "rb") as reads:
        count = sum(1 for _ in reads) // 4
    if count:
        logging.warning(
            f"{count:,} primary reads flagged neither first nor second of a pair "
            "(single-end) were left out of the paired FASTQ."
        )


@contextlib.contextmanager
def paired_fastq_stream(
    input_path: PathInput,
    *,
    threads: str,
    memory: str,
    temp_parent: PathInput,
    cram_opt: list[str] | None = None,
) -> Iterator[list[list[str]]]:
    """File extraction's collate/filtering with interleaved pairs on stdout."""
    with tempfile.TemporaryDirectory(
        dir=configured_temp_directory() or temp_parent, prefix=".collate_"
    ) as tmp:
        other = Path(tmp, "other.fastq.gz")
        yield _fastq_stages(
            input_path,
            "-",
            "-",
            threads=threads,
            memory=memory,
            temporary=tmp,
            singletons=devnull,
            out_other=other,
            cram_opt=cram_opt,
        )
        _warn_unpaired(other)


def alignments_to_fastq(
    input_path: PathInput,
    out_r1: PathInput,
    out_r2: PathInput,
    *,
    threads: str,
    memory: str,
    temp_parent: PathInput,
    cram_opt: list[str] | None = None,
    region: str | None = None,
    out_other: PathInput | None = None,
    out_singletons: PathInput | None = None,
    heartbeat: bool = False,
    expected_reads: int | None = None,
) -> None:
    """
    Pair up a BAM/CRAM's reads and write FASTQs (samtools view | collate | fastq).

    Standard collate groups mates in pseudo-random order, as a name sort does,
    but faster. Its fast mode (-f) would keep reads in genomic order, which skews
    aligners that estimate insert sizes per batch. Temp files go under
    ``temp_parent`` (the output directory) because a whole genome needs far more
    space than the system temp dir usually has. Singletons are discarded unless
    ``out_singletons`` is given; READ_OTHER reads go to ``out_other`` if given,
    else are discarded with a warning.
    """
    with (
        cleanup_files(
            on_failure=[
                out_r1,
                out_r2,
                *([out_other] if out_other else []),
                *([out_singletons] if out_singletons else []),
            ],
            preserve_outputs=atomic_outputs_enabled(),
        ),
        tempfile.TemporaryDirectory(
            dir=configured_temp_directory() or temp_parent, prefix=".collate_"
        ) as tmp,
        contextlib.ExitStack() as outputs,
    ):
        from wgsextract_cli.core.atomic_output import safe_output

        staged_r1 = outputs.enter_context(safe_output(out_r1))
        staged_r2 = outputs.enter_context(safe_output(out_r2))
        staged_other = (
            outputs.enter_context(safe_output(out_other)) if out_other else None
        )
        staged_singletons = (
            outputs.enter_context(safe_output(out_singletons))
            if out_singletons
            else None
        )
        run_pipeline(
            _fastq_stages(
                input_path,
                staged_r1,
                staged_r2,
                threads=threads,
                memory=memory,
                temporary=tmp,
                singletons=staged_singletons or Path(tmp, "singletons.fastq"),
                cram_opt=cram_opt,
                region=region,
                out_other=staged_other or Path(tmp, "other.fastq.gz"),
            ),
            stdout=subprocess.DEVNULL,
            description="FASTQ extraction",
            heartbeat=heartbeat,
            expected_reads=expected_reads,
        )
        if not staged_other:
            _warn_unpaired(Path(tmp, "other.fastq.gz"))
