import argparse
import contextlib
import gzip
import logging
import subprocess
import tempfile
import time
from array import array
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import TextIO

from wgsextract_cli.core import ultima
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.download_progress import CancelEvent
from wgsextract_cli.core.microarray_quality import quality_policy
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    match_chromosome_name,
    vcf_index_chromosomes,
)

from ._microarray_combined import _command_lines, _fasta_chromosomes, _open_text

# Reads with MAPQ under 10 could belong elsewhere in the genome. With them, a
# marker where every read has MAPQ 0 (a duplicated region, or ALT-unaware
# alignment) is called homozygous reference with no evidence; without them it
# is a no-call. On a real genome against its own chip, chr1-3: 99.54% -> 99.84%
# agreement, 0.7% fewer markers called.
MPILEUP_ARGS = ["-B", "-I", "-C", "50", "-q", "10"]


def split_snps_by_chrom(ref_vcf_tab: str, outdir: str) -> dict[str, str]:
    """Splits the master SNP list into per-chromosome files for parallel processing."""
    chrom_files: dict[str, str] = {}

    # We use tabix to list chromosomes efficiently if possible
    try:
        # Get list of chromosomes from the index
        res = run_command(["tabix", "-l", ref_vcf_tab], capture_output=True)
        chroms = res.stdout.strip().splitlines()

        for chrom in chroms:
            chrom_out = str(Path(outdir, f"snps_{chrom}.tab"))
            # tabix extraction is very fast
            with open(chrom_out, "w", newline="\n") as f:
                run_command(["tabix", ref_vcf_tab, chrom], stdout=f)
            chrom_files[chrom] = chrom_out

    except (
        OSError,
        gzip.BadGzipFile,
        subprocess.SubprocessError,
        WGSExtractError,
    ) as e:
        logging.warning(
            f"Failed to split SNPs using tabix: {e}. Falling back to linear scan."
        )
        # Fallback: one linear scan, one open handle per chromosome.
        chrom_files.clear()
        with contextlib.ExitStack() as stack:
            handles: dict[str, TextIO] = {}
            f_in = stack.enter_context(_open_text(ref_vcf_tab))
            for line in f_in:
                if line.startswith("#"):
                    continue
                chrom = line.split("\t", 1)[0]
                if chrom not in handles:
                    chrom_files[chrom] = str(Path(outdir, f"snps_{chrom}.tab"))
                    handles[chrom] = stack.enter_context(
                        open(chrom_files[chrom], "w", newline="\n")
                    )
                handles[chrom].write(line)

    return chrom_files


def _target_tab_chromosomes(ref_vcf_tab: str) -> list[str]:
    try:
        res = run_command(["tabix", "-l", ref_vcf_tab], capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Could not list target chromosomes for {ref_vcf_tab}: {e}")
        return []
    return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]


def _parallel_contigs(
    args: argparse.Namespace,
    threads: str,
    ref_vcf_tab: str,
    input_chroms: list[str] | None,
) -> list[str]:
    """Target contigs to call in separate workers, in alignment order, or none
    for one stream: parallel calling is the default with more than one thread."""
    parallel = getattr(args, "parallel", None)
    if parallel is None:
        parallel = int(threads) > 1
    if not parallel or args.region or not input_chroms:
        return []
    if not alignment_index(args.input):
        logging.info("Calling serially: per-chromosome calling needs an index.")
        return []
    targets = set(_target_tab_chromosomes(ref_vcf_tab))
    contigs = [chrom for chrom in input_chroms if chrom in targets]
    return contigs if len(contigs) > 1 else []


def _normalize_region_args_for_input(
    region_args: list[str], input_chroms: list[str]
) -> list[str]:
    if len(region_args) != 2 or region_args[0] != "-r" or not input_chroms:
        return region_args

    region = region_args[1]
    chrom, sep, rest = region.partition(":")
    match = match_chromosome_name(chrom, input_chroms)
    if not match:
        return region_args
    return ["-r", f"{match}{sep}{rest}"]


def _alignment_chromosomes(input_path: str, ref_fasta: str) -> list[str]:
    header = get_bam_header(input_path, ref_fasta)
    chroms: list[str] = []
    for line in header.splitlines():
        if not line.startswith("@SQ"):
            continue
        for field in line.split("\t")[1:]:
            if field.startswith("SN:"):
                chroms.append(field.removeprefix("SN:"))
                break
    return chroms


def _prepare_vcf_target_tab_for_input(
    ref_vcf_tab: str,
    input_vcf: str,
    outdir: str,
    input_chroms: list[str] | None = None,
) -> str:
    """Return a target tab whose chromosome names match the input."""
    if input_chroms is None:
        input_chroms = vcf_index_chromosomes(input_vcf)
    target_chroms = _target_tab_chromosomes(ref_vcf_tab)
    if not input_chroms or not target_chroms:
        return ref_vcf_tab

    input_chrom_set = set(input_chroms)
    chrom_map = {
        chrom: matched
        for chrom in target_chroms
        if (matched := match_chromosome_name(chrom, input_chrom_set))
    }
    if not chrom_map or all(chrom_map.get(chrom) == chrom for chrom in target_chroms):
        return ref_vcf_tab

    normalized_tab = str(Path(outdir, "input_chrom_targets.tab"))
    with (
        _open_text(ref_vcf_tab) as source,
        open(normalized_tab, "w", newline="\n") as destination,
    ):
        for line in source:
            if line.startswith("#") or not line.strip():
                destination.write(line)
                continue
            parts = line.rstrip("\n").split("\t")
            if not parts:
                continue
            mapped_chrom = chrom_map.get(parts[0])
            if not mapped_chrom:
                continue
            parts[0] = mapped_chrom
            destination.write("\t".join(parts) + "\n")

    normalized_tab_gz = normalized_tab + ".gz"
    run_command(["bgzip", "-f", normalized_tab])
    run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", normalized_tab_gz])
    logging.info(
        "Normalized microarray target chromosome names to match input "
        f"({len(chrom_map)} contigs)."
    )
    return normalized_tab_gz


def process_chrom(
    chrom: str,
    snp_file: str,
    chrom_tmp_dir: str,
    ref_fasta: str,
    ref_vcf_tab: str,
    ploidy_args: list[str],
    input_path: str,
    mpileup_args: list[str],
    call_format_args: list[str] | None = None,
    cancel_event: CancelEvent | None = None,
) -> tuple[str, int]:
    """Call one chromosome; returns its annotated VCF and last deep site."""
    chrom_vcf = str(Path(chrom_tmp_dir, f"{chrom}.vcf.gz"))
    logging.debug(f"Processing chromosome {chrom}...")
    run_pipeline(
        [
            ["bcftools", "mpileup", "-r", chrom, *mpileup_args]
            + ["-f", ref_fasta, "--targets-file", snp_file, input_path],
            ["bcftools", "call", *ploidy_args]
            + ["-m", "-V", "indels", *(call_format_args or []), "-Oz", "-o", chrom_vcf],
        ],
        stdout=subprocess.DEVNULL,
        description=f"Variant calling for {chrom}",
        cancel_event=cancel_event,
    )
    ensure_vcf_indexed(chrom_vcf)

    # Annotate RSIDs immediately
    annotated_chrom_vcf = str(Path(chrom_tmp_dir, f"{chrom}_ann.vcf.gz"))
    run_command(
        ["bcftools", "annotate", "-a", ref_vcf_tab, "-c", "CHROM,POS,ID"]
        + ["-Oz", "-o", annotated_chrom_vcf, chrom_vcf]
    )
    Path(chrom_vcf).unlink()
    Path(chrom_vcf + ".tbi").unlink()
    return annotated_chrom_vcf, _last_deep_position(annotated_chrom_vcf)


# htslib's errmod_cal randomly subsamples a site with more than 255 bases,
# drawing from one random stream that bcftools mpileup seeds once per process.
# Its INFO/DP counts every read before the base-quality filter, so it is never
# below the number of bases sampled from.
SUBSAMPLED_DEPTH = 255


def _last_deep_position(vcf: str) -> int:
    """The last site whose likelihoods depend on the random stream, or 0."""
    result = run_command(
        ["bcftools", "query", "-i", f"INFO/DP>{SUBSAMPLED_DEPTH}", "-f", "%POS\\n"]
        + [vcf],
        capture_output=True,
    )
    return max(map(int, (result.stdout or "").split()), default=0)


def _vcf_lines(vcf: str) -> tuple[list[str], list[str]]:
    with gzip.open(vcf, "rt", newline="\n") as source:
        lines = source.readlines()
    split = next((i for i, line in enumerate(lines) if line[0] != "#"), len(lines))
    return lines[:split], lines[split:]


def _recall_deep_contigs(
    deep: dict[str, int],
    chunks: dict[str, str],
    work_dir: str,
    *,
    ref_fasta: str,
    ref_vcf_tab: str,
    ploidy_args: list[str],
    input_path: str,
    mpileup_args: list[str],
    call_format_args: list[str],
) -> None:
    """Make per-contig calls identical to one stream.

    A contig worker restarts bcftools' subsampling stream, so its deep sites
    differ from a serial run once an earlier contig has drawn from that stream.
    Only deep sites draw from it, so one process over every contig with a deep
    site, in alignment order and each up to its last deep site, consumes the
    stream exactly as the serial run does. Every other site is already exact.
    """
    logging.info(
        f"Recalling {len(deep)} contigs with sites deeper than {SUBSAMPLED_DEPTH} "
        "reads in one stream, matching serial calling."
    )
    regions = ",".join(f"{chrom}:1-{last}" for chrom, last in deep.items())
    recalled = str(Path(work_dir, "deep.vcf.gz"))
    run_pipeline(
        [
            ["bcftools", "mpileup", "-r", regions, *mpileup_args]
            + ["-f", ref_fasta, "--targets-file", ref_vcf_tab, input_path],
            ["bcftools", "call", *ploidy_args]
            + ["-m", "-V", "indels", *call_format_args, "-Oz", "-o", recalled],
        ],
        stdout=subprocess.DEVNULL,
        description="Variant calling for deep sites",
    )
    annotated = str(Path(work_dir, "deep_ann.vcf.gz"))
    run_command(
        ["bcftools", "annotate", "-a", ref_vcf_tab, "-c", "CHROM,POS,ID"]
        + ["-Oz", "-o", annotated, recalled]
    )
    by_chrom: dict[str, list[str]] = {chrom: [] for chrom in deep}
    for line in _vcf_lines(annotated)[1]:
        by_chrom[line.split("\t", 1)[0]].append(line)

    for chrom, last in deep.items():
        header, records = _vcf_lines(chunks[chrom])
        tail = next(
            (i for i, line in enumerate(records) if int(line.split("\t", 2)[1]) > last),
            len(records),
        )
        positions = [line.split("\t", 2)[1] for line in records[:tail]]
        if positions != [line.split("\t", 2)[1] for line in by_chrom[chrom]]:
            raise WGSExtractError(
                f"Recalling deep sites on {chrom} produced different sites."
            )
        merged = str(Path(work_dir, f"{chrom}_deep.vcf"))
        with open(merged, "w", newline="\n") as destination:
            destination.writelines([*header, *by_chrom[chrom], *records[tail:]])
        run_command(["bgzip", "-f", merged])
        Path(merged + ".gz").replace(chunks[chrom])


# Reference-block ALTs: GATK, DeepVariant/bcftools, DRAGEN.
_REFERENCE_BLOCK_ALTS = {"<NON_REF>", "<*>", "."}
_EXPANDED = "WGSE_EXPANDED"


def _target_positions(ref_vcf_tab: str) -> dict[str, array[int]]:
    positions: dict[str, array[int]] = {}
    with _open_text(ref_vcf_tab) as targets:
        for line in targets:
            if line.strip() and not line.startswith("#"):
                chrom, pos, *_ = line.split("\t", 2)
                positions.setdefault(chrom, array("l")).append(int(pos))
    return {chrom: array("l", sorted(p)) for chrom, p in positions.items()}


def _expand_gvcf_targets(
    gvcf: str,
    out_vcf: str,
    *,
    ref_vcf_tab: str,
    ref_fasta: str,
    selection: list[str],
    outdir: str,
) -> None:
    """Write the gVCF's call at every target it covers. A reference block
    covering targets becomes one record per target (a 0/0 or ./. call with the
    FASTA's base as REF); other records overlapping a target are kept, with
    unobserved alleles such as <NON_REF> trimmed. Targets no record covers stay
    absent, i.e. uncalled. Only target positions are expanded: a whole-genome
    gVCF's blocks span billions of bases."""
    targets = _target_positions(ref_vcf_tab)
    fasta_chroms = _fasta_chromosomes(ref_fasta)
    fasta_names: dict[str, str | None] = {}
    expanded = 0
    with tempfile.TemporaryDirectory(prefix="gVCF_", dir=outdir) as work:
        overlapping = Path(work, "overlapping.vcf")
        records = Path(work, "records.vcf")
        regions = Path(work, "regions.txt")
        run_command(
            ["bcftools", "view", "--targets-file", ref_vcf_tab, "--targets-overlap"]
            + ["1", "--trim-alt-alleles", *selection]
            + ["-Ov", "-o", str(overlapping), gvcf]
        )
        # A variant record overlapping a block (DRAGEN writes some) outranks it.
        called: dict[str, set[int]] = {}
        with overlapping.open() as source:
            for line in source:
                if not line.startswith("#"):
                    chrom, start, _, _, alt, _, _, info, _ = line.split("\t", 8)
                    if alt not in _REFERENCE_BLOCK_ALTS or "END=" not in info:
                        called.setdefault(chrom, set()).add(int(start))
        # Pass 1: block records per target, REF left for one samtools pass.
        with (
            overlapping.open() as source,
            records.open("w", newline="\n") as out,
            regions.open("w", newline="\n") as wanted,
        ):
            for line in source:
                fields = line.rstrip("\n").split("\t")
                end = None
                if not line.startswith("#") and fields[4] in _REFERENCE_BLOCK_ALTS:
                    end = next(
                        (
                            int(item[4:])
                            for item in fields[7].split(";")
                            if item.startswith("END=")
                        ),
                        None,
                    )
                if end is not None and fields[0] not in fasta_names:
                    fasta_names[fields[0]] = match_chromosome_name(
                        fields[0], fasta_chroms
                    )
                fasta_chrom = fasta_names.get(fields[0])
                if end is None or not fasta_chrom:
                    out.write(line)
                    continue
                inside = targets.get(fields[0], array("l"))
                variants = called.get(fields[0], set())
                first = bisect_left(inside, int(fields[1]))
                for pos in inside[first : bisect_right(inside, end)]:
                    if pos in variants:
                        continue
                    fields[1], fields[7] = str(pos), _EXPANDED
                    out.write("\t".join(fields) + "\n")
                    wanted.write(f"{fasta_chrom}:{pos}-{pos}\n")
                    expanded += 1
        # Pass 2: fill in REF bases (faidx answers in request order).
        filled = Path(work, "filled.vcf")
        faidx = ["samtools", "faidx", "--region-file", str(regions), ref_fasta]
        bases = (
            line.strip().upper()
            for line in (_command_lines(faidx, work) if expanded else [])
            if not line.startswith(">")
        )
        with records.open() as source, filled.open("w", newline="\n") as out:
            for line in source:
                fields = line.split("\t", 8)
                if len(fields) > 7 and fields[7] == _EXPANDED:
                    fields[3], fields[7] = next(bases), "."
                    line = "\t".join(fields)
                out.write(line)
        run_pipeline(
            [
                ["bcftools", "sort", "-T", work, "-Ou", str(filled)],
                ["bcftools", "view", "--targets-file", ref_vcf_tab]
                + ["--targets-overlap", "1", "-Oz", "-o", out_vcf, "-"],
            ],
            description="gVCF target expansion",
        )


def _prepare_microarray_vcf(
    *,
    args: argparse.Namespace,
    outdir: str,
    base_name: str,
    is_vcf: bool,
    ref_vcf_tab: str,
    region_args: list[str],
    ploidy_args: list[str],
    ref_fasta: str,
    threads: str,
    start_vcf: float,
    out_vcf: str,
    input_chroms: list[str] | None = None,
    sample: str | None = None,
    gvcf: bool = False,
) -> tuple[str, str]:
    original_ref_vcf_tab = ref_vcf_tab

    try:
        if input_chroms is None:
            if is_vcf:
                ensure_vcf_indexed(args.input)
                input_chroms = vcf_index_chromosomes(args.input)
            else:
                input_chroms = _alignment_chromosomes(args.input, ref_fasta)

        if input_chroms:
            ref_vcf_tab = _prepare_vcf_target_tab_for_input(
                ref_vcf_tab, args.input, outdir, input_chroms
            )
            region_args = _normalize_region_args_for_input(region_args, input_chroms)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Microarray target normalization failed: {e}") from e

    mpileup_args = (
        [] if is_vcf else [*ultima.bcftools_options(args.input)[0], *MPILEUP_ARGS]
    )
    policy = quality_policy(args)
    call_format_args = []
    if not is_vcf and policy.needs_fields:
        mpileup_args += ["-a", "FORMAT/DP"]
        call_format_args = ["-f", "GQ"]
    if is_vcf:
        logging.info(f"VCF input detected. Extracting target SNPs from {args.input}...")
        # For VCF input, we intersect our targets with the input VCF.
        # Any missing targets are assumed to be homozygous reference.
        try:
            # Step 1: Get actual variants from the input VCF that match our targets
            hit_vcf = str(Path(outdir, f"{base_name}_hits.vcf.gz"))
            sample_args = ["--samples", sample, "--trim-alt-alleles"] if sample else []
            if gvcf:
                _expand_gvcf_targets(
                    args.input,
                    hit_vcf,
                    ref_vcf_tab=ref_vcf_tab,
                    ref_fasta=ref_fasta,
                    selection=sample_args + region_args,
                    outdir=outdir,
                )
            else:
                # Use long-form --targets-file to avoid version conflicts
                run_command(
                    ["bcftools", "view", "--targets-file", ref_vcf_tab]
                    + sample_args
                    + region_args
                    + ["-Oz", "-o", hit_vcf, args.input]
                )
            ensure_vcf_indexed(hit_vcf)

            # Step 2: Since we need a complete file for the templates, we'll
            # handle the "Reference Filling" during the extraction to CombinedKit.txt
            # to save the overhead of building a massive reference-filled VCF.
            out_vcf = hit_vcf  # We use the hits for extraction
            vcf_duration = time.time() - start_vcf
            logging.info(f"VCF extraction took {vcf_duration:.2f}s")

            # For VCF input, annotation is usually already present, but we run it
            # to ensure RSIDs match our master tab file
            annotated_vcf = str(Path(outdir, f"{base_name}_annotated.vcf.gz"))
            run_command(
                ["bcftools", "annotate", "-a", ref_vcf_tab, "-c", "CHROM,POS,ID"]
                + ["-Oz", "-o", annotated_vcf, out_vcf]
            )
            out_vcf = annotated_vcf
            ensure_vcf_indexed(out_vcf)

        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Microarray VCF extraction failed: {e}") from e

    elif contigs := _parallel_contigs(args, threads, ref_vcf_tab, input_chroms):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        workers = min(int(threads), len(contigs))
        logging.info(
            f"Calling {len(contigs)} chromosomes in parallel with {workers} workers..."
        )
        with tempfile.TemporaryDirectory(
            prefix="chrom_chunks_", dir=outdir
        ) as chrom_tmp_dir:
            chrom_snps = split_snps_by_chrom(ref_vcf_tab, chrom_tmp_dir)
            # Workers only drive bcftools, so threads suffice and library
            # callers need no __main__ guard; an API cancel stops their tools
            # through the shared process registry.
            stop = Event()
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    chrom: executor.submit(
                        process_chrom,
                        chrom,
                        chrom_snps[chrom],
                        chrom_tmp_dir,
                        ref_fasta,
                        ref_vcf_tab,
                        ploidy_args,
                        args.input,
                        mpileup_args,
                        call_format_args,
                        stop,
                    )
                    for chrom in contigs
                }
                try:
                    results = {
                        chrom: future.result() for chrom, future in futures.items()
                    }
                except BaseException:
                    stop.set()
                    for future in futures.values():
                        future.cancel()
                    raise
            vcf_chunks = {chrom: path for chrom, (path, _) in results.items()}
            deep = {chrom: last for chrom, (_, last) in results.items() if last}
            # The first contig with a deep site starts the stream as serial does.
            if len(deep) > 1:
                _recall_deep_contigs(
                    deep,
                    vcf_chunks,
                    chrom_tmp_dir,
                    ref_fasta=ref_fasta,
                    ref_vcf_tab=ref_vcf_tab,
                    ploidy_args=ploidy_args,
                    input_path=args.input,
                    mpileup_args=mpileup_args,
                    call_format_args=call_format_args,
                )

            logging.info(f"Merging {len(vcf_chunks)} chromosome VCFs...")
            # Alignment order, as one stream writes it.
            run_command(
                [
                    "bcftools",
                    "concat",
                    "--verbosity",
                    "0",
                    "-Oz",
                    "-o",
                    out_vcf,
                    *vcf_chunks.values(),
                ]  # fmt: skip
            )
        ensure_vcf_indexed(out_vcf)
        logging.info(
            f"Parallel VCF generation and annotation took {time.time() - start_vcf:.2f}s"
        )

    else:
        try:
            run_pipeline(
                [
                    ["bcftools", "mpileup", *region_args]
                    + [*mpileup_args, "-f", ref_fasta]
                    + ["--targets-file", ref_vcf_tab, args.input],
                    ["bcftools", "call", *ploidy_args]
                    + ["-m", "-V", "indels", *call_format_args, "-Oz", "-o", out_vcf],
                ],
                stdout=subprocess.DEVNULL,
                description="Microarray variant calling",
            )
            ensure_vcf_indexed(out_vcf)
            logging.info(f"VCF generation took {time.time() - start_vcf:.2f}s")

            # Annotate the VCF with RSIDs from the reference tab file
            annotated_vcf = str(Path(outdir, f"{base_name}_annotated.vcf.gz"))
            logging.info(f"Annotating VCF with RSIDs from {ref_vcf_tab}...")
            start_ann = time.time()
            run_command(
                ["bcftools", "annotate", "-a", ref_vcf_tab, "-c", "CHROM,POS,ID"]
                + ["-Oz", "-o", annotated_vcf, out_vcf]
            )
            out_vcf = annotated_vcf
            ensure_vcf_indexed(out_vcf)
            logging.info(f"Annotation took {time.time() - start_ann:.2f}s")
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Microarray variant calling failed: {e}") from e

    return out_vcf, original_ref_vcf_tab if is_vcf else ref_vcf_tab
