import argparse
import contextlib
import logging
import math
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from os import fspath
from pathlib import Path
from typing import NamedTuple

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import PathInput, absolute_path
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.samtools_commands import parse_memory
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
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


def get_base_args(args: argparse.Namespace) -> BaseArgs:
    """Validate --input, create the output directory and resolve the reference."""
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})
    logging.debug(f"Input file: {str(absolute_path(args.input))}")

    threads, memory = get_resource_defaults(args.threads, args.memory)
    outdir = args.outdir or str(absolute_path(args.input).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    try:
        resolved_ref = resolve_reference(args.ref, calculate_bam_md5(args.input, None))
    except (OSError, RuntimeError, ValueError) as e:
        raise WGSExtractError(f"Failed to resolve the reference: {e}") from e
    if resolved_ref and Path(resolved_ref).is_dir():
        logging.warning(
            f"No reference matching {args.input} was found in {resolved_ref}; "
            "continuing without one."
        )
        resolved_ref = None
    logging.debug(f"Resolved reference: {resolved_ref}")
    if resolved_ref:
        verify_paths_exist({"--ref": resolved_ref})

    cram_opt = ["-T", resolved_ref] if resolved_ref else []
    return BaseArgs(threads, memory, outdir, cram_opt, resolved_ref)


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
    temp: Sequence[PathInput] = (), on_failure: Sequence[PathInput] = ()
) -> Iterator[None]:
    """Always delete ``temp`` files; delete ``on_failure`` (partial outputs) only
    if the block raises, so they are never mistaken for complete results."""
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        for path in [*temp, *([] if succeeded else on_failure)]:
            with contextlib.suppress(FileNotFoundError):
                Path(path).unlink()


def resolve_region_or_gene(
    args: argparse.Namespace, resolved_ref: str | None
) -> str | None:
    """Return --region, or --gene resolved to coordinates; None if neither is set."""
    if args.region:
        return str(args.region)
    if not args.gene:
        return None

    from wgsextract_cli.core.config import settings
    from wgsextract_cli.core.gene_map import (
        gene_coords,
        gene_map_build,
        resolve_gene_map_reflib,
    )
    from wgsextract_cli.core.reference_resolver import ReferenceLibrary

    detected = ReferenceLibrary(
        resolved_ref, skip_full_search=True, input_path=args.input
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

    resolved_region = gene_coords(reflib_dir, args.gene, build)
    logging.info(f"Resolved gene {args.gene} to {resolved_region}")
    return str(resolved_region)


def _collate_buckets(input_path: PathInput, threads: str, memory: str) -> int:
    """Temp files for `samtools collate`, which holds one in memory at a time.

    Records in memory take about 70x their CRAM size and 20x their BAM size
    (generous for highly compressed files), so this keeps a bucket within the
    sort memory budget (threads x per-thread memory). At most 200 files stay
    under macOS's default limit of 256 open files.
    """
    try:
        size = Path(input_path).stat().st_size
    except OSError:
        size = 0
    in_memory = size * (70 if Path(input_path).suffix.lower() == ".cram" else 20)
    amount, unit = parse_memory(memory)
    budget = int(threads) * amount * (1024**3 if unit == "G" else 1024**2)
    return min(200, max(64, math.ceil(in_memory / budget)))


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
) -> None:
    """
    Pair up a BAM/CRAM's reads and write FASTQs (samtools view | collate | fastq).

    Standard collate groups mates in pseudo-random order, as a name sort does,
    but faster. Its fast mode (-f) would keep reads in genomic order, which skews
    aligners that estimate insert sizes per batch. Temp files go under
    ``temp_parent`` (the output directory) because a whole genome needs far more
    space than the system temp dir usually has. Singletons are discarded;
    READ_OTHER reads go to ``out_other`` if given.
    """
    with (
        cleanup_files(on_failure=[out_r1, out_r2, *([out_other] if out_other else [])]),
        tempfile.TemporaryDirectory(dir=temp_parent, prefix=".collate_") as tmp,
    ):
        view_cmd = ["samtools", "view", "-uh", "--no-PG", *(cram_opt or [])]
        view_cmd += [fspath(input_path), *([region] if region else [])]
        buckets = _collate_buckets(input_path, threads, memory)
        collate_cmd = ["samtools", "collate", "-u", "-O", "--no-PG", "-@", threads]
        collate_cmd += ["-n", str(buckets), "-T", str(Path(tmp, "collate")), "-"]
        fastq_cmd = ["samtools", "fastq", "-1", fspath(out_r1), "-2", fspath(out_r2)]
        fastq_cmd += ["-s", str(Path(tmp, "singletons.fastq"))]
        if out_other:
            fastq_cmd += ["-0", fspath(out_other)]
        fastq_cmd += ["-n", "-@", threads, "-"]
        run_pipeline(
            [view_cmd, collate_cmd, fastq_cmd],
            stdout=subprocess.DEVNULL,
            description="FASTQ extraction",
        )
