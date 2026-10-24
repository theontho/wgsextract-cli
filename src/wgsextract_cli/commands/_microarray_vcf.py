import argparse
import contextlib
import gzip
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TextIO

from wgsextract_cli.core.alignment_metadata import get_bam_header
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

from ._microarray_combined import _open_text

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
            with open(chrom_out, "w") as f:
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
                    handles[chrom] = stack.enter_context(open(chrom_files[chrom], "w"))
                handles[chrom].write(line)

    return chrom_files


def _target_tab_chromosomes(ref_vcf_tab: str) -> list[str]:
    try:
        res = run_command(["tabix", "-l", ref_vcf_tab], capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Could not list target chromosomes for {ref_vcf_tab}: {e}")
        return []
    return [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]


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
    with _open_text(ref_vcf_tab) as source, open(normalized_tab, "w") as destination:
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
) -> str:
    """Worker function for parallel chromosome processing. Must be at module level for pickling."""
    chrom_vcf = str(Path(chrom_tmp_dir, f"{chrom}.vcf.gz"))
    logging.debug(f"Processing chromosome {chrom}...")
    run_pipeline(
        [
            ["bcftools", "mpileup", "-r", chrom, *MPILEUP_ARGS]
            + ["-f", ref_fasta, "--targets-file", snp_file, input_path],
            ["bcftools", "call", *ploidy_args]
            + ["-m", "-V", "indels", "-Oz", "-o", chrom_vcf],
        ],
        stdout=subprocess.DEVNULL,
        description=f"Variant calling for {chrom}",
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
    return annotated_chrom_vcf


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
) -> tuple[str, str]:
    original_ref_vcf_tab = ref_vcf_tab

    try:
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

    if is_vcf:
        logging.info(f"VCF input detected. Extracting target SNPs from {args.input}...")
        # For VCF input, we intersect our targets with the input VCF.
        # Any missing targets are assumed to be homozygous reference.
        try:
            # Step 1: Get actual variants from the input VCF that match our targets
            hit_vcf = str(Path(outdir, f"{base_name}_hits.vcf.gz"))
            # Use long-form --targets-file to avoid version conflicts
            run_command(
                ["bcftools", "view", "--targets-file", ref_vcf_tab]
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

    elif args.parallel and not args.region:
        from concurrent.futures import ProcessPoolExecutor

        logging.info(
            f"Enabling parallel variant calling across chromosomes using {threads} threads..."
        )
        with tempfile.TemporaryDirectory(
            prefix="chrom_chunks_", dir=outdir
        ) as chrom_tmp_dir:
            chrom_snps = split_snps_by_chrom(ref_vcf_tab, chrom_tmp_dir)
            with ProcessPoolExecutor(max_workers=int(threads)) as executor:
                futures = [
                    executor.submit(
                        process_chrom,
                        chrom,
                        snp_file,
                        chrom_tmp_dir,
                        ref_fasta,
                        ref_vcf_tab,
                        ploidy_args,
                        args.input,
                    )
                    for chrom, snp_file in chrom_snps.items()
                ]
                vcf_chunks = [future.result() for future in futures]
            if not vcf_chunks:
                raise WGSExtractError("No VCF chunks were generated.")

            logging.info(f"Merging {len(vcf_chunks)} chromosome VCFs...")

            # Natural chromosome order (1..22, X, Y, MT) — bcftools concat preserves
            # input order, so lexical sort would emit 10, 11, ..., 1, 20, ..., 9.
            def _chrom_sort_key(path: str) -> tuple[int, str]:
                chrom = Path(path).name.split("_ann.")[0].removeprefix("chr")
                order = {"X": 23, "Y": 24, "MT": 25, "M": 25}
                if chrom in order:
                    return (order[chrom], chrom)
                try:
                    return (int(chrom), chrom)
                except ValueError:
                    return (99, chrom)

            run_command(
                ["bcftools", "concat", "-Oz", "-o", out_vcf]
                + sorted(vcf_chunks, key=_chrom_sort_key)
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
                    + [*MPILEUP_ARGS, "-f", ref_fasta]
                    + ["--targets-file", ref_vcf_tab, args.input],
                    ["bcftools", "call", *ploidy_args]
                    + ["-m", "-V", "indels", "-Oz", "-o", out_vcf],
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
