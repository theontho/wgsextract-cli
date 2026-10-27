"""Small-variant calling, with independent contigs sharing the worker budget."""

import gzip
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.reference_resolver import header_contig_lengths
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.utils import run_command, run_pipeline


def _cram_contigs(index: str, names: list[str]) -> set[str]:
    """CRAI reference IDs avoid launching workers for empty CRAM contigs."""
    try:
        with gzip.open(index, "rt") as source:
            ids = {int(line.split("\t", 1)[0]) for line in source if line.strip()}
        # -2 denotes a multi-reference slice, which may contain any contig.
        if -2 in ids or any(index >= len(names) or index < -2 for index in ids):
            return set(names)
        return {names[index] for index in ids if index >= 0}
    except (OSError, EOFError, ValueError):
        return set(names)


def _parallel_contigs(
    alignment: str, ref: str, threads: str, region: str | None
) -> dict[str, int]:
    # A region may overlap itself or request a different order; keep bcftools'
    # region semantics. Unindexed alignments still support sequential calling.
    index = alignment_index(alignment)
    if int(threads) <= 1 or region or not index:
        return {}
    if not Path(ref + ".fai").is_file():
        run_command(["samtools", "faidx", ref], capture_output=True)
    reference = read_fai_lengths(ref)
    alignment_contigs = list(header_contig_lengths(get_bam_header(alignment)))
    present = set(alignment_contigs)
    contigs = {
        contig: length for contig, length in reference.items() if contig in present
    }
    if list(contigs) != alignment_contigs:
        logging.info(
            "Calling serially to preserve the alignment's contig order "
            "(it differs from the reference index)."
        )
        return {}
    if alignment.lower().endswith(".cram") and index.lower().endswith(".crai"):
        populated = _cram_contigs(index, alignment_contigs)
        contigs = {
            name: length for name, length in contigs.items() if name in populated
        }
    return contigs if len(contigs) > 1 else {}


def _pipeline(
    alignment: str,
    ref: str,
    output: str,
    *,
    threads: str,
    region_args: list[str],
    profile: list[str],
    call_args: list[str],
    indels: bool,
) -> list[list[str]]:
    mpileup = ["bcftools", "mpileup", *profile, "-B"]
    if not indels:
        mpileup += ["-I"]
    mpileup += ["-C", "50", "-f", ref, "-Ou"]
    mpileup += [*region_args, alignment]
    call = [
        "bcftools", "call", *call_args, "-V", "snps" if indels else "indels",
        "-v", "-m", "--threads", threads,
    ]  # fmt: skip
    if not indels:
        return [mpileup, [*call, "-Oz", "-o", output]]
    return [
        mpileup,
        [*call, "-Ou"],
        ["bcftools", "norm", "-f", ref, "--threads", threads, "-Oz", "-o", output],
    ]


def call_small_variants(
    alignment: str,
    ref: str,
    output: str,
    *,
    threads: str,
    region: str | None,
    profile: list[str],
    call_args: list[str],
    indels: bool = False,
) -> None:
    contigs = _parallel_contigs(alignment, ref, threads, region)
    cancelled = Event() if contigs else None

    def call_part(
        part: str, region_args: list[str], label: str | None, compression_threads: str
    ) -> None:
        run_pipeline(
            _pipeline(
                alignment,
                ref,
                part,
                threads=compression_threads,
                region_args=region_args,
                profile=profile,
                call_args=call_args,
                indels=indels,
            ),
            description=f"{'InDel' if indels else 'SNP'} variant calling"
            + (f" ({label})" if label else ""),
            cancel_event=cancelled,
        )

    if not contigs:
        call_part(output, ["-r", region] if region else [], region, threads)
        return

    workers = min(int(threads), len(contigs))
    logging.info(f"Calling {len(contigs)} contigs with {workers} workers.")
    with tempfile.TemporaryDirectory(
        dir=Path(output).parent, prefix=".wgse_call_"
    ) as work:
        parts = [
            str(Path(work, f"{index:06d}.vcf.gz")) for index in range(len(contigs))
        ]
        regions = []
        for index, (contig, length) in enumerate(contigs.items()):
            # A TSV keeps names such as HLA-A*01:01:01:01 literal; -r would
            # interpret the colon as a coordinate separator.
            request = Path(work, f"{index:06d}.regions.tsv")
            request.write_text(
                f"{contig}\t1\t{length}\n", encoding="utf-8", newline="\n"
            )
            regions.append(["-R", str(request)])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(call_part, part, request, contig, "0")
                for part, request, contig in zip(parts, regions, contigs, strict=True)
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                if cancelled:
                    cancelled.set()
                for future in futures:
                    future.cancel()
                raise
        # A file list avoids Windows' command-line length limit for references
        # with thousands of scaffolds. Parts stay in .fai order, not finish order.
        file_list = Path(work, "parts.txt")
        file_list.write_text(
            "".join(f"{part}\n" for part in parts), encoding="utf-8", newline="\n"
        )
        merged = str(Path(work, "merged.vcf.gz"))
        run_command(
            [
                "bcftools",
                "concat",
                "--threads",
                threads,
                "-f",
                str(file_list),
                "-Oz",
                "-o",
                merged,
            ]  # fmt: skip
        )
        Path(merged).replace(output)
        for suffix in (".tbi", ".csi"):
            Path(output + suffix).unlink(missing_ok=True)
