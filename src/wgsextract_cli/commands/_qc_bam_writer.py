import logging
import subprocess
from heapq import heappop, heappush

from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import popen

from ._qc_commands import (
    SamWriter,
    SequenceProvider,
    _apply_fast_bam_variants,
    _fast_sam_record,
    _samtools_view_bam_writer_cmd,
    _write_sam_header,
)


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
