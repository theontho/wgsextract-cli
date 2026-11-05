"""Small-variant calling, optionally with contigs sharing the worker budget.

bcftools seeds one random stream per process and draws from it to subsample
every site with more than 255 bases (htslib ``errmod_cal``), so a deep site's
likelihoods depend on every deep site called before it in that process. A
per-contig worker starts that stream afresh, so ``--parallel`` SNP calling
replays the deep stretches of the genome in one process to match one stream:

1. Each worker also lists the columns at least ``_DENSE_DEPTH`` deep, read
   from mpileup's output before ``call -v`` hides the non-variant ones. For one
   sample, gVCF bands (``mpileup -g``) keep that listing cheap; ``call -v``
   skips their reference-only records unseen, so they change no call.
2. If more than one contig draws (has a column at least ``_DRAW_DEPTH`` deep),
   one process calls every stretch of dense columns that draws, in alignment
   order, after ``_LEAD_IN`` bases of shallower pileup that reproduce the reads
   mpileup's ``-d`` read cap keeps. No column outside these windows draws, so
   the replay draws the same numbers as one stream.
3. The replay's records replace the workers' inside the windows. Records that
   do not draw must agree; if any differ, the contigs that draw are replayed
   whole instead, which matches one stream by construction.

Indel calling never draws: bcftools computes indel likelihoods only below 250
reads (``--max-idepth``), and ``call -V snps`` drops the SNP sites that do
draw. Its per-contig workers therefore already match one stream.
"""

import bisect
import gzip
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Event, Thread
from typing import IO

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.reference_resolver import header_contig_lengths
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.utils import run_command, run_pipeline

# htslib's errmod_cal shuffles (draws for) a column's bases above 255.
_DRAW_DEPTH = 256
# Below this many bases a column's alive reads stay under mpileup's -d 250
# read cap, even when overlap detection discounts one mate of every pair.
_DENSE_DEPTH = 100
_LEAD_IN = 10_000
# Header lines that mpileup -g adds; call -v keeps no record that uses them.
_BAND_TAGS = ("##INFO=<ID=END,", "##INFO=<ID=MIN_DP,")

Window = tuple[str, int, int]


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
    bands: bool = False,
) -> list[list[str]]:
    mpileup = ["bcftools", "mpileup", *profile, "-B"]
    if not indels:
        mpileup += ["-I"]
    if bands:
        mpileup += ["-g", f"0,{_DENSE_DEPTH},{_DRAW_DEPTH}"]
    # Per-sample read counts (AD is only default from bcftools 1.21) let
    # confidence labels judge allele balance, also once multiallelics are split.
    mpileup += ["-a", "FORMAT/AD,FORMAT/DP", "-C", "50", "-f", ref, "-Ou"]
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
        # VCF requires IUPAC reference codes to resolve to a concrete base (R -> A).
        [
            "bcftools",
            "norm",
            "--check-ref",
            "ws",
            "-f",
            ref,
            "--threads",
            threads,
            "-Oz",
            "-o",
            output,
        ],  # fmt: skip
    ]


def _single_sample(alignment: str) -> bool:
    """Whether mpileup sees at most one sample, so gVCF bands, which group
    columns by their shallowest sample, list every deep column."""
    samples = set()
    for line in get_bam_header(alignment).splitlines():
        if line.startswith("@RG\t"):
            tags = dict(field.split(":", 1) for field in line.split("\t")[1:])
            if "SM" not in tags:
                return False
            samples.add(tags["SM"])
    return len(samples) <= 1


def _dense_listing(output: str) -> list[str]:
    return [
        "bcftools", "query", "-i", f"MAX(FMT/DP)>={_DENSE_DEPTH}",
        "-f", r"%CHROM\t%POS\t%END\t%REF\t%ALT[\t%DP\t%AD]\n", "-o", output,
    ]  # fmt: skip


@dataclass(frozen=True)
class _Dense:
    """A listed column, or a band of them, at least ``_DENSE_DEPTH`` deep."""

    line: str
    start: int
    end: int
    draws: bool


def _read_dense(path: str) -> dict[str, list[_Dense]]:
    dense: dict[str, list[_Dense]] = {}
    with open(path, encoding="utf-8") as listing:
        for line in listing:
            fields = line.rstrip("\n").split("\t")
            depth = max(int(dp) if dp.isdigit() else 0 for dp in fields[5::2])
            dense.setdefault(fields[0], []).append(
                _Dense(line, int(fields[1]), int(fields[2]), depth >= _DRAW_DEPTH)
            )
    return dense


def _drawing(dense: dict[str, list[_Dense]]) -> list[str]:
    return [row.line for rows in dense.values() for row in rows if row.draws]


def _windows(dense: dict[str, list[_Dense]]) -> list[Window]:
    """Each stretch of dense columns that draws, from ``_LEAD_IN`` bases before
    it to its last drawing column. Stretches closer than two lead-ins merge, so
    a lead-in never contains a dense column."""
    windows = []
    for contig, rows in dense.items():
        clusters: list[list[_Dense]] = []
        for row in rows:
            if clusters and row.start - clusters[-1][-1].end <= 2 * _LEAD_IN:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        for cluster in clusters:
            draws = [row.end for row in cluster if row.draws]
            if draws:
                windows.append(
                    (contig, max(1, cluster[0].start - _LEAD_IN), max(draws))
                )
    return windows


def _pump(
    source: list[str],
    sinks: Sequence[list[list[str]]],
    copy: Callable[[IO[bytes], list[IO[bytes]]], None],
    *,
    description: str,
    cancel_event: Event | None,
) -> None:
    """Run ``source`` and let ``copy`` feed its output to each sink pipeline."""
    source_read, source_write = os.pipe()
    pipes = [os.pipe() for _ in sinks]
    errors: list[BaseException] = []

    def run(stages: list[list[str]], stdin: int | None, stdout: int | None) -> None:
        own = stdout if stdin is None else stdin
        assert own is not None
        try:
            run_pipeline(
                stages,
                stdin=stdin,
                stdout=stdout,
                description=description,
                cancel_event=cancel_event,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            # Our copy of the child's end: closing it lets the reader see EOF,
            # or the writer see a broken pipe, once the child has gone.
            os.close(own)

    threads = [Thread(target=run, args=([source], None, source_write))]
    threads += [
        Thread(target=run, args=(stages, read, None))
        for stages, (read, _) in zip(sinks, pipes, strict=True)
    ]
    for thread in threads:
        thread.start()
    outputs: list[IO[bytes]] = [open(write, "wb") for _, write in pipes]
    failure: BaseException | None = None
    try:
        with open(source_read, "rb") as stream:
            copy(stream, outputs)
    except BaseException as error:
        failure = error
    finally:
        for output in outputs:
            try:
                output.close()
            except OSError as error:
                failure = failure or error
        for thread in threads:
            thread.join()
    # A sink that exits early breaks the pipe; its own error says why.
    if errors:
        raise errors[0]
    if failure:
        raise failure


def _tee(source: IO[bytes], outputs: list[IO[bytes]]) -> None:
    while chunk := source.read(1 << 20):
        for output in outputs:
            output.write(chunk)


def _pos(record: bytes) -> int:
    return int(record.split(b"\t", 2)[1])


class _Splice:
    """Copy a worker's records, replacing those inside ``windows`` with the
    replay's, and note whether the records at columns that do not draw agree."""

    def __init__(
        self,
        windows: list[Window],
        replay: list[bytes],
        dense: list[_Dense],
        verify: bool,
    ) -> None:
        self.windows = [(start, end) for _, start, end in windows]
        self.replay = replay
        self.draws = [(row.start, row.end) for row in dense if row.draws]
        self.verify = verify
        self.agrees = True

    @staticmethod
    def _within(spans: list[tuple[int, int]], pos: int) -> bool:
        index = bisect.bisect_right(spans, pos, key=lambda span: span[0]) - 1
        return index >= 0 and pos <= spans[index][1]

    def _undrawn(self, lines: list[bytes]) -> list[bytes]:
        return [line for line in lines if not self._within(self.draws, _pos(line))]

    def __call__(self, source: IO[bytes], outputs: list[IO[bytes]]) -> None:
        (output,) = outputs
        replay = iter(self.replay)
        pending = next(replay, None)
        replaced = []
        for line in source:
            if line.startswith(b"#"):
                output.write(line)
                continue
            pos = _pos(line)
            if self._within(self.windows, pos):
                if self.verify:
                    replaced.append(line)
                continue
            while pending is not None and _pos(pending) < pos:
                output.write(pending)
                pending = next(replay, None)
            output.write(line)
        while pending is not None:
            output.write(pending)
            pending = next(replay, None)
        if self.verify:
            self.agrees = self._undrawn(replaced) == self._undrawn(self.replay)


@dataclass
class _ContigCaller:
    alignment: str
    ref: str
    profile: list[str]
    call_args: list[str]
    indels: bool
    bands: bool = False
    workers: int = 1
    cancelled: Event | None = None

    @property
    def description(self) -> str:
        return "InDel variant calling" if self.indels else "SNP variant calling"

    def call(
        self,
        output: str,
        region_args: list[str],
        label: str | None,
        threads: str,
        listing: str | None = None,
    ) -> None:
        """Call into ``output``, also listing dense columns into ``listing``."""
        stages = _pipeline(
            self.alignment,
            self.ref,
            output,
            threads=threads,
            region_args=region_args,
            profile=self.profile,
            call_args=self.call_args,
            indels=self.indels,
            bands=self.bands,
        )
        description = self.description + (f" ({label})" if label else "")
        if listing is None:
            run_pipeline(stages, description=description, cancel_event=self.cancelled)
        else:
            _pump(
                stages[0], [stages[1:], [_dense_listing(listing)]], _tee,
                description=description, cancel_event=self.cancelled,
            )  # fmt: skip

    def run_all(self, tasks: Sequence[Callable[[], None]]) -> None:
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(task) for task in tasks]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                if self.cancelled:
                    self.cancelled.set()
                for future in futures:
                    future.cancel()
                raise

    def match_one_stream(
        self,
        contigs: dict[str, int],
        parts: list[str],
        listings: list[str],
        work: str,
    ) -> list[str]:
        """``parts``, with the columns that draw called as one stream would."""
        dense: dict[str, list[_Dense]] = {}
        for listing in listings:
            dense.update(_read_dense(listing))
        drawing = [name for name, rows in dense.items() if any(r.draws for r in rows)]
        # A worker's stream is one stream's until a second contig draws.
        if len(drawing) < 2:
            return parts
        spliced = self._replay(
            list(contigs), parts, dense, _windows(dense), Path(work, "windows")
        )
        if spliced is None:
            logging.warning(
                "Deep-site replay windows disagreed with the per-contig calls; "
                f"replaying the {len(drawing)} contigs with deep sites whole."
            )
            whole = [(name, 1, contigs[name]) for name in drawing]
            spliced = self._replay(
                list(contigs), parts, dense, whole, Path(work, "contigs"), verify=False
            )
        assert spliced is not None
        return spliced

    def _replay(
        self,
        names: list[str],
        parts: list[str],
        dense: dict[str, list[_Dense]],
        windows: list[Window],
        work: Path,
        *,
        verify: bool = True,
    ) -> list[str] | None:
        """Call ``windows`` in one process and splice them into ``parts``, or
        None if a deep column's depth or a record that does not draw differs."""
        work.mkdir()
        request = work / "regions.tsv"
        request.write_text(
            "".join(f"{contig}\t{start}\t{end}\n" for contig, start, end in windows),
            encoding="utf-8", newline="\n",
        )  # fmt: skip
        bases = sum(end - start + 1 for _, start, end in windows)
        logging.info(
            f"Calling {len(windows)} deep stretches ({bases:,} bp) in one process "
            "so their subsampling matches a single stream."
        )
        calls, listing = str(work / "calls.vcf.gz"), str(work / "dense.tsv")
        self.call(calls, ["-R", str(request)], "deep sites", "0", listing)
        if verify and _drawing(_read_dense(listing)) != _drawing(dense):
            return None
        records: dict[str, list[bytes]] = {}
        with gzip.open(calls, "rb") as replay:
            for line in replay:
                if not line.startswith(b"#"):
                    records.setdefault(line.split(b"\t", 1)[0].decode(), []).append(
                        line
                    )
        by_contig: dict[str, list[Window]] = {}
        for window in windows:
            by_contig.setdefault(window[0], []).append(window)
        spliced = list(parts)
        splices: dict[int, _Splice] = {}
        for index, name in enumerate(names):
            if name in by_contig:
                splices[index] = _Splice(
                    by_contig[name], records.get(name, []), dense.get(name, []), verify
                )
                spliced[index] = str(work / f"{index:06d}.bcf")

        def splice(index: int) -> None:
            _pump(
                ["bcftools", "view", "--no-version", parts[index]],
                [[["bcftools", "view", "--no-version", "-Ou", "-o", spliced[index]]]],
                splices[index],
                description=f"Splicing deep-site calls ({names[index]})",
                cancel_event=self.cancelled,
            )

        self.run_all([partial(splice, index) for index in splices])
        if not all(item.agrees for item in splices.values()):
            return None
        return spliced


def _drop_band_header(merged: str, work: str) -> str:
    header = run_command(["bcftools", "view", "-h", merged], capture_output=True)
    lines = [
        line for line in header.stdout.splitlines() if not line.startswith(_BAND_TAGS)
    ]
    header_file = Path(work, "header.txt")
    header_file.write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
    )
    result = str(Path(work, "reheadered.vcf.gz"))
    run_command(["bcftools", "reheader", "-h", str(header_file), "-o", result, merged])
    return result


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
    parallel: bool = False,
) -> None:
    contigs = _parallel_contigs(alignment, ref, threads, region) if parallel else {}
    caller = _ContigCaller(alignment, ref, profile, call_args, indels)
    if not contigs:
        caller.call(output, ["-r", region] if region else [], region, threads)
        return

    replays = not indels
    caller.bands = replays and _single_sample(alignment)
    caller.workers = min(int(threads), len(contigs))
    caller.cancelled = Event()
    logging.info(f"Calling {len(contigs)} contigs with {caller.workers} workers.")
    with tempfile.TemporaryDirectory(
        dir=Path(output).parent, prefix=".wgse_call_"
    ) as work:
        parts, listings, tasks = [], [], []
        for index, (contig, length) in enumerate(contigs.items()):
            # A TSV keeps names such as HLA-A*01:01:01:01 literal; -r would
            # interpret the colon as a coordinate separator.
            request = Path(work, f"{index:06d}.regions.tsv")
            request.write_text(
                f"{contig}\t1\t{length}\n", encoding="utf-8", newline="\n"
            )
            parts.append(str(Path(work, f"{index:06d}.vcf.gz")))
            listings.append(str(Path(work, f"{index:06d}.dense.tsv")))
            tasks.append(
                partial(
                    caller.call,
                    parts[-1],
                    ["-R", str(request)],
                    contig,
                    "0",
                    listings[-1] if replays else None,
                )  # fmt: skip
            )
        caller.run_all(tasks)
        if replays:
            parts = caller.match_one_stream(contigs, parts, listings, work)
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
        if caller.bands:
            merged = _drop_band_header(merged, work)
        Path(merged).replace(output)
        for suffix in (".tbi", ".csi"):
            Path(output + suffix).unlink(missing_ok=True)
