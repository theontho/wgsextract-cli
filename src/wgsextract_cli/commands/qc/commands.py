import argparse
import logging
import os
import subprocess
from collections.abc import Callable

from wgsextract_cli.core import (
    runtime_wrappers,
)
from wgsextract_cli.core.dependencies import _tool_command_parts, get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)

from ..vcf.basic import _select_vcf_input

SamWriter = Callable[[str], None]


SequenceProvider = Callable[[int, int, int], str]


_FAST_BAM_VARIANT_SPACING = 2000


def cmd_fastp(args: argparse.Namespace) -> None:
    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves FASTQ inputs.")

    from wgsextract_cli.core.variant_files import verify_paths_exist

    paths = {"--r1": args.r1}
    if args.r2:
        paths["--r2"] = args.r2
    if not verify_paths_exist(paths):
        return

    verify_dependencies(["fastp"])
    log_dependency_info(["fastp"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.r1))

    logging.debug(f"Input file (R1): {os.path.abspath(args.r1)}")
    if args.r2:
        logging.debug(f"Input file (R2): {os.path.abspath(args.r2)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    base_name = os.path.basename(args.r1).split(".")[0]
    out_r1 = os.path.join(outdir, f"{base_name}_fp_1.fastq.gz")
    out_json = os.path.join(outdir, f"{base_name}_fastp.json")
    out_html = os.path.join(outdir, f"{base_name}_fastp.html")

    cmd = [
        "fastp",
        "--thread",
        threads,
        "-i",
        args.r1,
        "-o",
        out_r1,
        "-j",
        out_json,
        "-h",
        out_html,
    ]
    if args.r2:
        out_r2 = os.path.join(outdir, f"{base_name}_fp_2.fastq.gz")
        cmd.extend(["-I", args.r2, "-O", out_r2])

    logging.info(LOG_MESSAGES["running_fastp"].format(input=args.r1))
    try:
        run_command(cmd)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"fastp failed: {e}")
        raise WGSExtractError("fastp failed.") from e


def cmd_fastqc(args: argparse.Namespace) -> None:
    if not args.input:
        logging.error(LOG_MESSAGES["input_required"])
        return

    from wgsextract_cli.core.variant_files import verify_paths_exist

    if not verify_paths_exist({"--input": args.input}):
        return

    verify_dependencies(["fastqc"])
    log_dependency_info(["fastqc"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(args.input))
    )

    logging.debug(f"Input file: {os.path.abspath(args.input)}")
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")

    logging.info(LOG_MESSAGES["running_fastqc"].format(input=args.input))
    try:
        run_command(["fastqc", "-t", threads, "-o", outdir, args.input])
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"FastQC failed: {e}")
        raise WGSExtractError("FastQC failed.") from e


def cmd_vcf_qc(args: argparse.Namespace) -> None:
    input_file = _select_vcf_input(args)
    if not input_file:
        logging.error("--input is required.")
        return

    from wgsextract_cli.core.variant_files import verify_paths_exist

    if not verify_paths_exist({"--input": input_file}):
        return

    verify_dependencies(["bcftools"])
    log_dependency_info(["bcftools"])

    logging.debug(f"Input file: {os.path.abspath(input_file)}")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    base_name = os.path.basename(input_file)
    out_stats = os.path.join(outdir, f"{base_name}.vcfstats.txt")

    logging.info(LOG_MESSAGES["vcf_stats"].format(input=input_file, output=out_stats))
    try:
        with open(out_stats, "w") as f:
            run_command(["bcftools", "stats", input_file], stdout=f)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"VCF stats failed: {e}") from e


def _reference_backed_sequence_provider(
    ref_path: str | None,
    chroms: dict[str, int],
    fallback: SequenceProvider,
) -> SequenceProvider:
    if not ref_path or not os.path.exists(str(ref_path)):
        return fallback

    fai_path = str(ref_path) + ".fai"
    if not os.path.exists(fai_path):
        try:
            run_command(["samtools", "faidx", str(ref_path)], capture_output=True)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as exc:
            logging.warning(
                f"Reference FASTA is not indexed; falling back to synthetic read sequence: {exc}"
            )
            return fallback

    logging.info(
        "Using reference-backed read sequence with deterministic SNP simulation..."
    )
    chrom_names = list(chroms)
    chunk_size = 16 * 1024 * 1024
    unavailable_chroms: set[str] = set()
    cache_chrom = ""
    cache_start = 0
    cache_seq = ""

    def fetch_reference_chunk(chrom: str, chunk_start: int) -> tuple[int, str] | None:
        chunk_end = min(chroms[chrom], chunk_start + chunk_size)
        region = f"{chrom}:{chunk_start + 1}-{chunk_end}"
        result = run_command(
            ["samtools", "faidx", str(ref_path), region],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            unavailable_chroms.add(chrom)
            logging.warning(
                f"Reference does not provide {chrom}; using synthetic read sequence for that contig."
            )
            return None
        seq = "".join(
            line.strip()
            for line in (result.stdout or "").splitlines()
            if line and not line.startswith(">")
        ).upper()
        return chunk_start, seq

    def get_reference_seq(chrom_idx: int, pos: int, length: int) -> str:
        nonlocal cache_chrom, cache_start, cache_seq

        chrom = chrom_names[chrom_idx]
        if chrom in unavailable_chroms:
            return fallback(chrom_idx, pos, length)

        pieces = []
        cursor = pos
        remaining = length
        while remaining > 0:
            if not (
                cache_chrom == chrom
                and cache_start <= cursor
                and cursor < cache_start + len(cache_seq)
            ):
                chunk_start = (cursor // chunk_size) * chunk_size
                fetched = fetch_reference_chunk(chrom, chunk_start)
                if fetched is None:
                    return fallback(chrom_idx, pos, length)
                cache_start, cache_seq = fetched
                cache_chrom = chrom

            chunk_offset = cursor - cache_start
            take = min(remaining, len(cache_seq) - chunk_offset)
            if take <= 0:
                return fallback(chrom_idx, pos, length)
            pieces.append(cache_seq[chunk_offset : chunk_offset + take])
            cursor += take
            remaining -= take

        return "".join(pieces)

    return get_reference_seq


def _samtools_view_bam_writer_cmd(bam_path: str, threads: str) -> list[str]:
    samtools = get_tool_path("samtools") or "samtools"
    cmd = _tool_command_parts(samtools) + [
        "view",
        "-@",
        threads,
        "-1",
        "-b",
        "-o",
        bam_path,
        "-",
    ]
    return runtime_wrappers.wrap_command(cmd)


def _write_sam_header(
    write_sam: SamWriter,
    chroms: dict[str, int],
    target_md5: str | None,
) -> None:
    write_sam("@HD\tVN:1.6\tSO:coordinate\n")
    rg_line = "@RG\tID:sample1\tSM:sample1\tPL:ILLUMINA"
    if target_md5:
        rg_line += f"\tDS:MD5:{target_md5}"
    write_sam(rg_line + "\n")

    if target_md5:
        write_sam(f"@CO\tMD5:{target_md5}\n")

    for name, length in chroms.items():
        write_sam(f"@SQ\tSN:{name}\tLN:{length}\n")


def _fast_sam_record(
    read_id: str,
    flag: int,
    chrom: str,
    pos: int,
    mate_pos: int,
    template_length: int,
    cigar: str,
    seq: str,
    qual: str,
    nm: int = 0,
) -> str:
    return (
        f"{read_id}\t{flag}\t{chrom}\t{pos}\t60\t{cigar}\t=\t{mate_pos}\t"
        f"{template_length}\t{seq}\t{qual}\tRG:Z:sample1\tNM:i:{nm}\n"
    )


def _first_fast_bam_variant_pos(chrom_idx: int, seed: int) -> int:
    return 100 + ((seed + chrom_idx * 97) % (_FAST_BAM_VARIANT_SPACING - 100))


def _fast_bam_alt_base(ref_base: str, chrom_idx: int, pos: int, seed: int) -> str:
    alternatives = [base for base in "ACGT" if base != ref_base.upper()]
    return alternatives[(pos + seed + chrom_idx * 13) % len(alternatives)]


def _apply_fast_bam_variants(
    seq: str,
    chrom_idx: int,
    pos0: int,
    seed: int,
) -> tuple[str, int]:
    """Apply deterministic homozygous SNPs without storing a genome-wide map."""
    first_read_pos = pos0 + 1
    last_read_pos = pos0 + len(seq)
    first_variant = _first_fast_bam_variant_pos(chrom_idx, seed)
    if first_variant > last_read_pos:
        return seq, 0

    if first_variant < first_read_pos:
        steps = (
            first_read_pos - first_variant + _FAST_BAM_VARIANT_SPACING - 1
        ) // _FAST_BAM_VARIANT_SPACING
        first_variant += steps * _FAST_BAM_VARIANT_SPACING

    if first_variant > last_read_pos:
        return seq, 0

    mutated = list(seq)
    mismatch_count = 0
    variant_pos = first_variant
    while variant_pos <= last_read_pos:
        read_offset = variant_pos - first_read_pos
        ref_base = mutated[read_offset].upper()
        if ref_base in "ACGT":
            mutated[read_offset] = _fast_bam_alt_base(
                ref_base, chrom_idx, variant_pos, seed
            )
            mismatch_count += 1
        variant_pos += _FAST_BAM_VARIANT_SPACING

    return "".join(mutated), mismatch_count
