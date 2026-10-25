import logging
import subprocess
from collections.abc import Callable, Iterator
from heapq import heappop, heappush
from pathlib import Path

from wgsextract_cli.core import runtime_wrappers
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.tool_commands import _tool_command_parts
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command

SamWriter = Callable[[str], object]
SequenceProvider = Callable[[int, int, int], str]

_FAST_BAM_VARIANT_SPACING = 2000


def _reference_backed_sequence_provider(
    ref_path: str | None,
    chroms: dict[str, int],
    fallback: SequenceProvider,
) -> SequenceProvider:
    if not ref_path or not Path(str(ref_path)).exists():
        return fallback

    fai_path = str(ref_path) + ".fai"
    if not Path(fai_path).exists():
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


def fast_bam_snps(
    chrom_idx: int, length: int, seed: int, ref_base: SequenceProvider
) -> Iterator[tuple[int, str, str]]:
    """(1-based position, REF, ALT) of each homozygous SNP the streaming BAM
    plants in a contig of ``length``: the fake VCF's truth."""
    for pos in range(
        _first_fast_bam_variant_pos(chrom_idx, seed),
        length + 1,
        _FAST_BAM_VARIANT_SPACING,
    ):
        ref = ref_base(chrom_idx, pos - 1, 1).upper()
        if ref in ("A", "C", "G", "T"):
            yield pos, ref, _fast_bam_alt_base(ref, chrom_idx, pos, seed)


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
    except (OSError, subprocess.SubprocessError) as exc:
        raise WGSExtractError(f"Failed to write fake BAM {bam_path}: {exc}") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


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
