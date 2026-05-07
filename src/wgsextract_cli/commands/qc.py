import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from heapq import heappop, heappush

from wgsextract_cli.core import runtime
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import (
    get_tool_path,
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    get_sam_index_cmd,
    get_sam_view_cmd,
    popen,
    run_command,
)

SamWriter = Callable[[str], None]
SequenceProvider = Callable[[int, int, int], str]

_FAST_BAM_VARIANT_SPACING = 2000


def _select_vcf_input(args):
    input_path = getattr(args, "input", None)
    vcf_input = getattr(args, "vcf_input", None)
    default_vcf = settings.get("default_input_vcf")
    explicit_dests: set[str] = getattr(args, "_explicit_dests", set())

    if vcf_input and vcf_input != default_vcf:
        return vcf_input
    if "input" in explicit_dests and input_path:
        return input_path
    return vcf_input if vcf_input else input_path


def register(subparsers, base_parser):
    parser = subparsers.add_parser(
        "qc", help="Runs quality control or calculates coverage."
    )
    qc_subs = parser.add_subparsers(dest="qc_cmd", required=True)

    fastp_parser = qc_subs.add_parser(
        "fastp", parents=[base_parser], help=CLI_HELP["cmd_fastp"]
    )
    fastp_parser.add_argument("--r1", help=CLI_HELP["arg_r1"])
    fastp_parser.add_argument("--r2", help=CLI_HELP["arg_r2"])
    fastp_parser.set_defaults(func=cmd_fastp)

    fastqc_parser = qc_subs.add_parser(
        "fastqc", parents=[base_parser], help=CLI_HELP["cmd_fastqc"]
    )
    fastqc_parser.set_defaults(func=cmd_fastqc)

    vcf_parser = qc_subs.add_parser(
        "vcf", parents=[base_parser], help=CLI_HELP["cmd_vcf-qc"]
    )
    vcf_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help=CLI_HELP["arg_vcf_input"],
    )
    vcf_parser.set_defaults(func=cmd_vcf_qc)

    fake_parser = qc_subs.add_parser(
        "fake-data", parents=[base_parser], help=CLI_HELP["cmd_fake-data"]
    )
    fake_parser.add_argument(
        "--coverage", type=float, default=1.0, help="Coverage depth (e.g. 30.0)"
    )
    fake_parser.add_argument(
        "--build",
        choices=["hg38", "hg19", "hg37", "t2t"],
        default="hg38",
        help="Human genome build naming convention.",
    )
    fake_parser.add_argument(
        "--type",
        default="cram",
        help="Comma-separated list of types to generate (vcf, cram, bam, fastq, all). Default: cram",
    )
    fake_parser.add_argument(
        "--full-size",
        action="store_true",
        help="Use real human chromosome lengths. The default scaled mode uses shorter chromosomes.",
    )
    fake_parser.add_argument(
        "--legacy-bam",
        action="store_true",
        help=(
            "Use the older scaled fake BAM generator. Slower and unavailable with --full-size, "
            "but includes randomized placement and indel CIGARs."
        ),
    )
    fake_parser.add_argument(
        "--seed", type=int, default=42, help="Random seed for reproducibility"
    )
    fake_parser.set_defaults(func=cmd_fake_data)


def cmd_fastp(args):
    if not args.r1:
        raise WGSExtractError("--r1 is required unless --genome resolves FASTQ inputs.")

    from wgsextract_cli.core.utils import verify_paths_exist

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
    except Exception as e:
        logging.error(f"fastp failed: {e}")
        raise WGSExtractError("fastp failed.") from e


def cmd_fastqc(args):
    if not args.input:
        logging.error(LOG_MESSAGES["input_required"])
        return

    from wgsextract_cli.core.utils import verify_paths_exist

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
    except Exception as e:
        logging.error(f"FastQC failed: {e}")
        raise WGSExtractError("FastQC failed.") from e


def cmd_vcf_qc(args):
    input_file = _select_vcf_input(args)
    if not input_file:
        logging.error("--input is required.")
        return

    from wgsextract_cli.core.utils import verify_paths_exist

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
    except Exception as e:
        raise WGSExtractError(f"VCF stats failed: {e}") from e


def _tool_command_parts(cmd_base: str) -> list[str]:
    if (
        runtime.is_wsl_tool_command(cmd_base)
        or runtime.is_bundled_tool_command(cmd_base)
        or runtime.is_pacman_tool_command(cmd_base)
    ):
        return [cmd_base]
    if os.path.exists(cmd_base):
        return [cmd_base]
    return shlex.split(cmd_base)


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
    return runtime.wrap_command(cmd)


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
        except Exception as exc:
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


def _stream_fast_bam_sam(
    write_sam: SamWriter,
    chroms: dict[str, int],
    coverage: float,
    seed: int,
    target_md5: str | None,
    get_noise_seq: SequenceProvider,
) -> None:
    """Write coordinate-sorted, paired-end SAM without materializing reads in memory."""
    _write_sam_header(write_sam, chroms, target_md5)

    base_read_len = 100
    base_insert_size = 300
    qual_cache: dict[int, str] = {}

    for chrom_idx, (name, length) in enumerate(chroms.items()):
        num_pairs = max(2, int((length * coverage) / (base_read_len * 2)))
        max_start = max(1, length - base_insert_size - 1)
        step = max_start / max(1, num_pairs - 1)
        pending_mates: list[tuple[int, str]] = []

        for pair_idx in range(num_pairs):
            pos1 = min(max_start, max(1, int(pair_idx * step) + 1))
            jitter = ((pair_idx + seed + chrom_idx * 17) % 11) - 5
            rl1 = max(75, min(125, base_read_len + (jitter % 7) - 3))
            rl2 = max(75, min(125, base_read_len - (jitter % 7) + 3))
            insert_size = max(rl1 + rl2 + 10, base_insert_size + jitter)
            pos2 = min(length - rl2 + 1, pos1 + insert_size - rl2)
            pos2 = max(pos1, pos2)

            while pending_mates and pending_mates[0][0] <= pos1:
                _, mate_line = heappop(pending_mates)
                write_sam(mate_line)

            read_id = f"read_{name}_{pair_idx}"
            r1_seq, r1_nm = _apply_fast_bam_variants(
                get_noise_seq(chrom_idx, pos1 - 1, rl1), chrom_idx, pos1 - 1, seed
            )
            r2_seq, r2_nm = _apply_fast_bam_variants(
                get_noise_seq(chrom_idx, pos2 - 1, rl2), chrom_idx, pos2 - 1, seed
            )
            q1 = qual_cache.setdefault(rl1, "I" * rl1)
            q2 = qual_cache.setdefault(rl2, "I" * rl2)
            r1_cigar = f"{rl1}M"
            r2_cigar = f"{rl2}M"

            write_sam(
                _fast_sam_record(
                    read_id,
                    99,
                    name,
                    pos1,
                    pos2,
                    insert_size,
                    r1_cigar,
                    r1_seq,
                    q1,
                    r1_nm,
                )
            )
            heappush(
                pending_mates,
                (
                    pos2,
                    _fast_sam_record(
                        read_id,
                        147,
                        name,
                        pos2,
                        pos1,
                        -insert_size,
                        r2_cigar,
                        r2_seq,
                        q2,
                        r2_nm,
                    ),
                ),
            )

        while pending_mates:
            _, mate_line = heappop(pending_mates)
            write_sam(mate_line)


def _create_fast_fake_bam(
    bam_path: str,
    chroms: dict[str, int],
    coverage: float,
    seed: int,
    target_md5: str | None,
    get_noise_seq: SequenceProvider,
    threads: str,
) -> None:
    logging.info(f"Streaming coordinate-sorted fake BAM directly to {bam_path}...")
    cmd = _samtools_view_bam_writer_cmd(bam_path, threads)
    process = popen(cmd, stdin=subprocess.PIPE)
    stdin = process.stdin
    if stdin is None:
        raise WGSExtractError("Failed to open samtools stdin for fake BAM creation.")

    pending = bytearray()
    flush_at = 4 * 1024 * 1024

    def write_sam(line: str) -> None:
        pending.extend(line.encode())
        if len(pending) >= flush_at:
            stdin.write(pending)
            pending.clear()

    try:
        _stream_fast_bam_sam(
            write_sam,
            chroms,
            coverage,
            seed,
            target_md5,
            get_noise_seq,
        )
        if pending:
            stdin.write(pending)
        stdin.close()
        return_code = process.wait()
        if return_code != 0:
            raise WGSExtractError(
                f"samtools failed while creating fake BAM with exit code {return_code}."
            )
    except Exception:
        if process.poll() is None:
            process.kill()
        raise


def _write_fake_reference(
    ref_path: str, chroms: dict[str, int], get_noise_seq: SequenceProvider
) -> None:
    with open(ref_path, "w") as f:
        for idx, (name, length) in enumerate(chroms.items()):
            f.write(f">{name}\n")
            chunk_size = 1000000
            for i in range(0, length, chunk_size):
                this_chunk_len = min(chunk_size, length - i)
                seq = list(get_noise_seq(idx, i, this_chunk_len))
                f.write("".join(seq) + "\n")


def cmd_fake_data(args):
    verify_dependencies(["samtools", "bcftools", "bgzip", "tabix"])
    log_dependency_info(["samtools", "bcftools"])

    outdir = args.outdir if args.outdir else os.getcwd()
    os.makedirs(outdir, exist_ok=True)

    from wgsextract_cli.core.ref_library import get_available_genomes
    from wgsextract_cli.core.utils import resolve_reference

    # Try to find reference in library if a known build is specified
    lib_ref = None
    target_md5 = None

    # Map CLI builds to library codes
    build_map = {"hg38": "hg38", "hg19": "hg19", "hg37": "hs37d5", "t2t": "T2Tv20"}

    target_code = build_map.get(args.build)
    if target_code:
        all_genomes = get_available_genomes()
        genome_info = next(
            (
                g
                for g in all_genomes
                if g["code"] == target_code or g["final"].startswith(target_code)
            ),
            None,
        )
        if genome_info:
            target_md5 = genome_info.get("md5") if genome_info.get("md5") else None
            if target_md5:
                logging.debug(f"Found target MD5 for {args.build}: {target_md5}")
            # See if it's installed
            from wgsextract_cli.core.config import settings

            reflib_dir = settings.get("reference_library")
            if reflib_dir:
                candidate = os.path.join(reflib_dir, "genomes", genome_info["final"])
                if os.path.exists(candidate):
                    lib_ref = candidate

    ref_path = resolve_reference(args.ref, None) if args.ref else lib_ref

    # If the resolved path is still a directory, it means it didn't find a fasta file there.
    if ref_path and os.path.isdir(ref_path):
        ref_path = None

    # Parse types
    types = [t.strip().lower() for t in args.type.split(",")]
    if "all" in types:
        types = ["vcf", "cram", "bam", "fastq"]

    generate_fake_genomics_data(
        outdir,
        ref_path,
        coverage=args.coverage,
        seed=args.seed,
        build=args.build,
        full_size=args.full_size,
        types=types,
        target_md5=target_md5,
        legacy_bam=args.legacy_bam,
    )


def generate_fake_genomics_data(
    outdir,
    ref_path=None,
    coverage=1.0,
    seed=42,
    build="hg38",
    full_size=False,
    types=None,
    target_md5=None,
    legacy_bam=False,
):
    """Generates a scaled-down or full human fake BAM, CRAM and VCF."""
    import random

    if types is None:
        types = ["cram"]

    random.seed(seed)

    if legacy_bam and full_size:
        raise WGSExtractError("--legacy-bam is only supported for scaled fake data.")

    mode = "Full size" if full_size else "Scaled"
    logging.info(
        f"Generating fake human-like genomics data ({build}, {mode}) in {outdir} (Coverage: {coverage}x)..."
    )
    if target_md5:
        logging.debug(f"Generator using target MD5: {target_md5}")

    is_hg19 = build in ["hg19", "hg37"]

    # Pre-generate a noise buffer to use for both FASTA and BAM
    # 1MB of noise is enough to avoid obvious patterns
    noise_size = 1024 * 1024
    noise_buffer = "".join(random.choices(["A", "C", "G", "T"], k=noise_size))

    def get_noise_seq(chrom_idx, pos, length):
        # Use a large prime offset per chromosome to ensure diversity
        offset = chrom_idx * 15485863  # A large prime
        start = (pos + offset) % noise_size
        if start + length <= noise_size:
            return noise_buffer[start : start + length]
        else:
            # Wrap around
            return noise_buffer[start:] + noise_buffer[: length - (noise_size - start)]

    # Chromosome lengths
    if full_size:
        if build == "t2t":
            chroms = {
                "chr1": 248387328,
                "chr2": 242696752,
                "chr3": 201105948,
                "chr4": 193574945,
                "chr5": 182045439,
                "chr6": 172126628,
                "chr7": 160567428,
                "chr8": 146259331,
                "chr9": 150617247,
                "chr10": 134758134,
                "chr11": 135127769,
                "chr12": 133324548,
                "chr13": 113566686,
                "chr14": 101161492,
                "chr15": 99753195,
                "chr16": 96330374,
                "chr17": 84276017,
                "chr18": 80542538,
                "chr19": 61707364,
                "chr20": 66210255,
                "chr21": 45090682,
                "chr22": 57938617,
                "chrX": 154259566,
                "chrY": 62460029,
                "chrM": 16569,
            }
        elif is_hg19:
            chroms = {
                "1": 249250621,
                "2": 243199373,
                "3": 198022430,
                "4": 191154276,
                "5": 180915260,
                "6": 171115067,
                "7": 159138663,
                "8": 146364022,
                "9": 141213431,
                "10": 135534747,
                "11": 135006516,
                "12": 133851895,
                "13": 115169878,
                "14": 107349540,
                "15": 102531392,
                "16": 90354753,
                "17": 81195210,
                "18": 78077248,
                "19": 59128983,
                "20": 63025520,
                "21": 48129895,
                "22": 51304566,
                "X": 155270560,
                "Y": 59373566,
                "MT": 16569,
            }
        else:
            chroms = {
                "chr1": 248956422,
                "chr2": 242193529,
                "chr3": 198295559,
                "chr4": 190214555,
                "chr5": 181538259,
                "chr6": 170805979,
                "chr7": 159345973,
                "chr8": 145138636,
                "chr9": 138394717,
                "chr10": 133797422,
                "chr11": 135086622,
                "chr12": 133275309,
                "chr13": 114364328,
                "chr14": 107043718,
                "chr15": 101991189,
                "chr16": 90338345,
                "chr17": 83257441,
                "chr18": 80373285,
                "chr19": 58617616,
                "chr20": 64444167,
                "chr21": 46709983,
                "chr22": 50818468,
                "chrX": 156040895,
                "chrY": 57227415,
                "chrM": 16569,
            }

        # Calculate estimated BAM size to warn user
        # 1x WGS BAM is roughly 3GB.
        est_bam_gb = 3 * coverage
        if est_bam_gb > 1:
            logging.warning(
                f"Generating {est_bam_gb:.1f}GB of fake data. This will take significant time and disk space."
            )
            # Check free space
            import shutil

            _, _, free = shutil.disk_usage(outdir)
            if free < (est_bam_gb * 1.5) * (1024**3):
                logging.error(
                    f"Insufficient disk space in {outdir}. Need at least {est_bam_gb * 1.5:.1f}GB."
                )
                return
    else:
        chroms = {}
        for i in range(1, 23):
            name = f"chr{i}" if not is_hg19 else str(i)
            chroms[name] = 500000 + (i * 1000)  # variety
        chroms["chrX" if not is_hg19 else "X"] = 600000
        chroms["chrY" if not is_hg19 else "Y"] = 400000
        chroms["chrM" if not is_hg19 else "MT"] = 16569  # Real length for chrM

    # Add a dummy contig to avoid collision with known SN counts in info.py (e.g. 25)
    chroms["chrExtra" if not is_hg19 else "Extra"] = 1000

    # 0. Pre-generate consistent variants only for the legacy scaled BAM path.
    # The default streaming path applies deterministic SNPs in-flight without a
    # chromosome-wide variant map so it works at full-genome scale.
    need_bam = any(t in types for t in ["bam", "cram", "fastq"])
    use_streaming_bam = need_bam and not legacy_bam
    consistent_variants = {}
    if need_bam and not use_streaming_bam:
        # This ensures that all reads covering a position see the same variant
        # variants[chrom] = {pos: (ref, alt, is_indel, cigar_change)}
        for name, length in chroms.items():
            v_list = {}
            # 1 variant every 2000 bp
            num_v = max(2, length // 2000)
            for _ in range(num_v):
                v_pos = random.randint(100, length - 100)
                v_type = random.random()
                if v_type < 0.8:  # SNP
                    v_list[v_pos] = (
                        random.choice("ACGT"),
                        random.choice("ACGT"),
                        False,
                    )
                else:  # Indel (just a marker for now, we'll do real ones if possible)
                    v_list[v_pos] = (random.choice("ACGT"), "AT", True)
            consistent_variants[name] = v_list

    ref_path_was_provided = bool(ref_path)

    # 1. Create a reference if none provided
    if not ref_path:
        ref_path = os.path.join(
            outdir, f"fake_ref_{build}_{mode.lower().replace(' ', '_')}.fa"
        )

    # Preserve scaled fake-data behavior by creating a small reference, but avoid
    # writing a full human FASTA unless a caller explicitly requested it or CRAM
    # output requires a reference.
    should_create_reference = ref_path_was_provided or not full_size or "cram" in types
    ref_exists = os.path.exists(str(ref_path))
    if ref_exists:
        logging.info(f"Using reference: {ref_path}")
    elif should_create_reference:
        logging.info(f"Creating fake reference at {ref_path}...")
        _write_fake_reference(str(ref_path), chroms, get_noise_seq)
        run_command(["samtools", "faidx", ref_path])
    else:
        logging.info(
            "Skipping full-size fake reference creation because the requested outputs "
            "do not require a reference."
        )

    # 2. Create fake BAM with reads on all chromosomes based on coverage
    # We generate reads in sorted order to avoid a massive sort operation
    sam_path = os.path.join(outdir, "fake.sam")
    bam_path = os.path.join(outdir, "fake.bam")
    base_read_len = 100
    base_insert_size = 300

    if need_bam:
        if use_streaming_bam:
            threads, _ = get_resource_defaults(None, None)
            _create_fast_fake_bam(
                bam_path,
                chroms,
                coverage,
                seed,
                target_md5,
                _reference_backed_sequence_provider(ref_path, chroms, get_noise_seq),
                threads,
            )
        else:
            with open(sam_path, "w") as f:
                f.write("@HD\tVN:1.6\tSO:coordinate\n")

                # Embed MD5 in Read Group Description so it survives into BAM and is visible to samtools view -H
                rg_line = "@RG\tID:sample1\tSM:sample1\tPL:ILLUMINA"
                if target_md5:
                    rg_line += f"\tDS:MD5:{target_md5}"
                f.write(rg_line + "\n")

                if target_md5:
                    f.write(f"@CO\tMD5:{target_md5}\n")

                # Always write SQ lines from chroms dict for fake data
                for name, length in chroms.items():
                    f.write(f"@SQ\tSN:{name}\tLN:{length}\n")

                # Generate reads chromosome by chromosome (sorted)
                for idx, (name, length) in enumerate(chroms.items()):
                    num_pairs = int(
                        (length * coverage) / (base_read_len * 2)
                    )  # divided by 2 for pairs
                    if num_pairs < 2:
                        num_pairs = 2

                    # Generate read pairs
                    reads = []
                    cv = consistent_variants.get(name, {})
                    for i in range(num_pairs):
                        # Randomize read length and insert size
                        rl1 = int(random.gauss(base_read_len, 2))
                        rl2 = int(random.gauss(base_read_len, 2))
                        # Ensure lengths are reasonable
                        rl1 = max(50, min(150, rl1))
                        rl2 = max(50, min(150, rl2))

                        ins = int(random.gauss(base_insert_size, 15))
                        ins = max(rl1 + rl2 + 10, ins)  # Ensure no weird overlaps

                        pos1 = random.randint(1, length - ins - 100)
                        pos2 = pos1 + ins - rl2

                        read_id = f"read_{name}_{i}"

                        # Pull sequences from the deterministic noise buffer
                        r1_seq_list = list(get_noise_seq(idx, pos1 - 1, rl1))
                        r2_seq_list = list(get_noise_seq(idx, pos2 - 1, rl2))

                        # Apply consistent variants
                        # Homozygous for smoke tests to ensure reliable calling
                        r1_cigar = f"{rl1}M"
                        r2_cigar = f"{rl2}M"

                        # Check R1
                        for v_pos, v_data in cv.items():
                            v_ref, v_val, is_indel = v_data
                            if pos1 <= v_pos < pos1 + rl1:
                                rel_pos = v_pos - pos1
                                if not is_indel:
                                    # Ensure rel_pos is valid after possible previous indels
                                    if rel_pos < len(r1_seq_list):
                                        # Ensure alt is different from ref
                                        if r1_seq_list[rel_pos] == v_val:
                                            r1_seq_list[rel_pos] = (
                                                "A" if v_val != "A" else "C"
                                            )
                                        else:
                                            r1_seq_list[rel_pos] = v_val
                                elif rel_pos > 10 and rel_pos < len(r1_seq_list) - 20:
                                    # Real Deletion: 5bp
                                    del_seq = (
                                        r1_seq_list[:rel_pos]
                                        + r1_seq_list[rel_pos + 5 :]
                                    )
                                    r1_seq_list = del_seq
                                    r1_cigar = (
                                        f"{rel_pos}M5D{len(r1_seq_list) - rel_pos}M"
                                    )

                        # Check R2
                        for v_pos, v_data in cv.items():
                            v_ref, v_val, is_indel = v_data
                            if pos2 <= v_pos < pos2 + rl2:
                                rel_pos = v_pos - pos2
                                if not is_indel:
                                    if rel_pos < len(r2_seq_list):
                                        if r2_seq_list[rel_pos] == v_val:
                                            r2_seq_list[rel_pos] = (
                                                "A" if v_val != "A" else "C"
                                            )
                                        else:
                                            r2_seq_list[rel_pos] = v_val
                                elif rel_pos > 10 and rel_pos < len(r2_seq_list) - 20:
                                    del_seq = (
                                        r2_seq_list[:rel_pos]
                                        + r2_seq_list[rel_pos + 5 :]
                                    )
                                    r2_seq_list = del_seq
                                    r2_cigar = (
                                        f"{rel_pos}M5D{len(r2_seq_list) - rel_pos}M"
                                    )

                        r1_seq = "".join(r1_seq_list)
                        r2_seq = "".join(r2_seq_list)

                        # R1 (99 = paired, proper pair, mstrand, mate reverse)
                        reads.append(
                            (
                                pos1,
                                f"{read_id}\t99\t{name}\t{pos1}\t60\t{r1_cigar}\t=\t{pos2}\t{ins}\t"
                                + r1_seq
                                + "\t"
                                + "I" * len(r1_seq)
                                + "\tRG:Z:sample1\n",
                            )
                        )
                        # R2 (147 = paired, proper pair, reverse, mate mstrand)
                        reads.append(
                            (
                                pos2,
                                f"{read_id}\t147\t{name}\t{pos2}\t60\t{r2_cigar}\t=\t{pos1}\t-{ins}\t"
                                + r2_seq
                                + "\t"
                                + "I" * len(r2_seq)
                                + "\tRG:Z:sample1\n",
                            )
                        )

                    # Sort all reads by position
                    reads.sort()

                    # Write in batches
                    batch_size = 10000
                    for i in range(0, len(reads), batch_size):
                        f.write("".join([r[1] for r in reads[i : i + batch_size]]))

            # Convert SAM to BAM (already sorted)
            run_command(
                get_sam_view_cmd(threads="1", fmt="BAM", is_input_sam=True)
                + [sam_path, "-o", bam_path]
            )

        run_command(get_sam_index_cmd(bam_path))
        if os.path.exists(sam_path):
            os.remove(sam_path)
        logging.info(f"Created {bam_path} ({len(chroms)} chromosomes)")

    # 3. Create fake CRAM
    if "cram" in types:
        cram_path = os.path.join(outdir, "fake.cram")
        run_command(
            get_sam_view_cmd(threads="1", fmt="CRAM", reference=ref_path)
            + [bam_path, "-o", cram_path]
        )
        run_command(get_sam_index_cmd(cram_path))
        logging.info(f"Created {cram_path}")

    # 4. Create fake VCF with variants on all chroms
    if "vcf" in types:
        vcf_path = os.path.join(outdir, "fake.vcf")
        with open(vcf_path, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
            # Use our chroms list directly for consistency
            for name, length in chroms.items():
                f.write(f"##contig=<ID={name},length={length}>\n")

            f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n")

            # At 30x, density = 700. At 1x, density = 21000.
            if full_size:
                variant_density = max(700, int(21000 / max(1.0, coverage)))
            else:
                variant_density = 5000

            for name, length in chroms.items():
                num_variants = int(length / variant_density)
                if num_variants < 2:
                    num_variants = 2

                positions = sorted(
                    [random.randint(1, length) for _ in range(num_variants)]
                )

                batch_size = 50000
                for i in range(0, len(positions), batch_size):
                    batch = positions[i : i + batch_size]
                    lines = []
                    for pos in batch:
                        # Randomized REF/ALT using the seeded random generator
                        ref = random.choice(["A", "C", "G", "T"])
                        alt = random.choice(
                            [b for b in ["A", "C", "G", "T"] if b != ref]
                        )

                        lines.append(
                            f"{name}\t{pos}\t.\t{ref}\t{alt}\t100\tPASS\t.\tGT\t0/1\n"
                        )
                    f.write("".join(lines))

        vcf_gz = vcf_path + ".gz"
        with open(vcf_gz, "wb") as f_gz:
            run_command(["bgzip", "-c", vcf_path], stdout=f_gz)
        run_command(["tabix", "-p", "vcf", vcf_gz])
        os.remove(vcf_path)
        logging.info(f"Created {vcf_gz}")

    # 5. Create fake FASTQ (R1 and R2)
    if "fastq" in types:
        r1_path = os.path.join(outdir, "fake_R1.fastq.gz")
        r2_path = os.path.join(outdir, "fake_R2.fastq.gz")

        # We can use samtools fastq to generate these from the BAM we just made
        # -1 and -2 for paired end
        run_command(
            [
                "samtools",
                "fastq",
                "-1",
                r1_path,
                "-2",
                r2_path,
                "-0",
                "/dev/null",  # Ignore any singleton reads
                "-s",
                "/dev/null",  # Ignore any shared reads
                bam_path,
            ]
        )
        logging.info(f"Created {r1_path}")
        logging.info(f"Created {r2_path}")

    # Cleanup intermediate BAM if not requested
    if need_bam and "bam" not in types:
        if os.path.exists(bam_path):
            os.remove(bam_path)
        if os.path.exists(bam_path + ".bai"):
            os.remove(bam_path + ".bai")
        logging.debug(f"Removed intermediate BAM {bam_path}")
