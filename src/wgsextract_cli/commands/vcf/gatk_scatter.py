"""Opt-in interval scattering; assembly boundaries can change GATK calls."""

import logging
import math
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event

import psutil

from wgsextract_cli.core.alignment_memory import available_memory
from wgsextract_cli.core.paths import java_local_path
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.resource_estimates import GATK_WORKER_BYTES
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

Interval = tuple[str, int, int]


def scatter_intervals(
    reference: str, region: str | None, threads: str
) -> list[list[Interval]]:
    """Disjoint, approximately equal-sized shards in reference dictionary order."""
    lengths = read_fai_lengths(reference)
    if not lengths:
        raise WGSExtractError("GATK --scatter requires a reference FASTA index.")
    requested: dict[str, list[tuple[int, int]]] = {}
    for text in region.split(",") if region else lengths:
        text = text.strip()
        if not text:
            continue
        if text in lengths:
            contig, start, end = text, 1, lengths[text]
        else:
            contig, _, span = text.rpartition(":")
            try:
                first, separator, last = span.partition("-")
                start = int(first)
                end = int(last) if separator else start
                if contig not in lengths or not 1 <= start <= end <= lengths[contig]:
                    raise ValueError
            except ValueError as exc:
                raise WGSExtractError(f"Invalid GATK scatter region: {text!r}") from exc
        requested.setdefault(contig, []).append((start, end))
    intervals: list[Interval] = []
    for contig in lengths:
        for start, end in sorted(requested.get(contig, [])):
            if (
                intervals
                and intervals[-1][0] == contig
                and start <= intervals[-1][2] + 1
            ):
                old_contig, old_start, old_end = intervals[-1]
                intervals[-1] = old_contig, old_start, max(old_end, end)
            else:
                intervals.append((contig, start, end))
    try:
        memory_workers = max(1, available_memory() // GATK_WORKER_BYTES)
    except (OSError, ValueError):
        logging.warning(
            "Available RAM unknown; limiting GATK scattering to one worker."
        )
        memory_workers = 1
    workers = min(int(threads), memory_workers)
    if workers < int(threads):
        logging.warning(
            "Limiting GATK scattering to %s worker(s): allow 6 GiB RAM per worker.",
            workers,
        )
    total = sum(end - start + 1 for _, start, end in intervals)
    if not total:
        raise WGSExtractError("GATK --scatter selected no reference bases.")
    target = math.ceil(total / workers)
    shards: list[list[Interval]] = [[]]
    used = 0
    for contig, start, end in intervals:
        while start <= end:
            if used == target:
                shards.append([])
                used = 0
            stop = min(end, start + target - used - 1)
            shards[-1].append((contig, start, stop))
            used += stop - start + 1
            start = stop + 1
    return shards


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    """Reap the launcher and its Java children, including on native Windows."""
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(children, timeout=2)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=2)
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_worker(command: list[str], log: Path, cancelled: Event) -> None:
    if cancelled.is_set():
        return
    with log.open("wb") as stream:
        process = popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                if cancelled.wait(0.1):
                    raise WGSExtractError("GATK scattering cancelled.")
            if process.returncode:
                with log.open(errors="replace") as source:
                    tail = "".join(deque(source, maxlen=20)).strip()
                raise WGSExtractError(
                    f"GATK scatter worker failed (exit {process.returncode}):\n{tail}"
                )
        finally:
            if process.poll() is None:
                _stop_worker(process)


def call_scattered(
    command: list[str],
    shards: list[list[Interval]],
    output: str,
    work: Path,
    gatk_tmp: str,
) -> None:
    """Gather only after every worker succeeds; preserve an existing final VCF."""
    cancelled = Event()
    parts = []
    commands = []
    for index, intervals in enumerate(shards):
        prefix = f"{index:06d}"
        request = work / f"{prefix}.intervals"
        request.write_text(
            "".join(f"{contig}:{start}-{end}\n" for contig, start, end in intervals),
            encoding="utf-8",
            newline="\n",
        )
        part = work / f"{prefix}.vcf.gz"
        parts.append(part)
        temporary = Path(gatk_tmp, prefix)
        temporary.mkdir()
        commands.append(
            [
                command[0],
                "--java-options",
                "-Xmx4g -XX:ActiveProcessorCount=1 -XX:ParallelGCThreads=1 "
                "-XX:ConcGCThreads=1",
                *command[1:],
                "--native-pair-hmm-threads",
                "1",
                "--tmp-dir",
                java_local_path(temporary),
                "-L",
                java_local_path(request),
                "-O",
                java_local_path(part),
            ]
        )
    logging.warning(
        "GATK interval scattering is opt-in: assembly boundaries can change calls."
    )
    logging.info(
        "Calling %s GATK shards with one CPU and a 4 GiB heap each.", len(shards)
    )
    logging.debug("GATK scatter intervals: %s", shards)
    with ThreadPoolExecutor(max_workers=len(shards)) as pool:
        futures = [
            pool.submit(_run_worker, cmd, work / f"{index:06d}.log", cancelled)
            for index, cmd in enumerate(commands)
        ]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            cancelled.set()
            for future in futures:
                future.cancel()
            raise
    file_list = work / "parts.txt"
    file_list.write_text(
        "".join(f"{part}\n" for part in parts), encoding="utf-8", newline="\n"
    )
    merged = str(work / "merged.vcf.gz")
    run_command(["bcftools", "concat", "-f", str(file_list), "-Oz", "-o", merged])
    ensure_vcf_indexed(merged)
    Path(merged).replace(output)
    for suffix in (".tbi", ".csi"):
        Path(output + suffix).unlink(missing_ok=True)
        source = Path(merged + suffix)
        if source.exists():
            source.replace(output + suffix)
