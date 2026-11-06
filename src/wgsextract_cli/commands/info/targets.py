"""Per-target depth of a BED panel and its on- versus off-target enrichment
(``info calculate-coverage --by``), as after Nanopore adaptive sampling.

Two samtools passes with the same read filters as ``--bins`` (default flags,
no base or mapping quality minimum, deletions not counted, both overlapping
mates counted): ``samtools depth -a -b`` streams every targeted base into a
depth histogram per target, and ``samtools bedcov -j`` sums the depth of the
targets and of everything else for the enrichment.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from wgsextract_cli.core.results import Scalar, current_result, human_output, warn
from wgsextract_cli.core.table_writer import Column, Value, table_path, write_table
from wgsextract_cli.core.utils import UsageError, WGSExtractError, popen, run_command

from .render import _cram_reference_opts

DEPTHS = (10, 20, 30)
MITOCHONDRIA = frozenset({"chrM", "MT", "chrMT", "M"})


@dataclass
class BedTarget:
    name: str
    chrom: str
    start: int  # 0-based
    end: int
    histogram: dict[int, int] = field(default_factory=dict)

    @property
    def length(self) -> int:
        return self.end - self.start

    def bases_at_least(self, depth: int) -> int:
        return sum(n for d, n in self.histogram.items() if d >= depth)

    @property
    def depth_sum(self) -> int:
        return sum(d * n for d, n in self.histogram.items())

    @property
    def mean(self) -> float:
        return self.depth_sum / self.length

    @property
    def median(self) -> float:
        """The middle depth (the mean of the two middle ones for even lengths)."""
        ranks = {(self.length - 1) // 2, self.length // 2}
        return sum(_nth(self.histogram, r) for r in ranks) / len(ranks)


def _nth(histogram: Mapping[int, int], rank: int) -> int:
    """The depth of the ``rank``-th (0-based) base in depth order."""
    seen = 0
    for depth in sorted(histogram):
        seen += histogram[depth]
        if rank < seen:
            return depth
    raise AssertionError("rank beyond the histogram")


def _contig_aliases(name: str) -> list[str]:
    bare = name[3:] if name.lower().startswith("chr") else name
    if bare.upper() in ("M", "MT"):
        return ["chrM", "MT", "chrMT", "M"]
    return [f"chr{bare}", bare]


def read_bed(path: Path, lengths: Mapping[str, int]) -> list[BedTarget]:
    """BED targets in the alignment's contig names (chr1 and 1, chrM and MT
    are matched); unnamed ones are named contig:start-end (1-based)."""
    targets: list[BedTarget] = []
    renamed: dict[str, str] = {}
    unknown: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise UsageError(f"Cannot read --by {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.startswith(("#", "track", "browser")):
            continue
        fields = line.split("\t")
        try:
            chrom, start, end = fields[0], int(fields[1]), int(fields[2])
        except (IndexError, ValueError):
            raise UsageError(
                f"--by {path} line {number} is not BED (contig, start, end): "
                f"{line[:80]!r}"
            ) from None
        if not 0 <= start < end:
            raise UsageError(f"--by {path} line {number}: start must be below end.")
        contig = chrom
        if contig not in lengths:
            contig = next((a for a in _contig_aliases(chrom) if a in lengths), "")
            if not contig:
                unknown.add(chrom)
                continue
            renamed[chrom] = contig
        if end > lengths[contig]:
            raise UsageError(
                f"--by {path} line {number}: {chrom}:{end} is beyond the end of "
                f"{contig} ({lengths[contig]:,} bp); is the BED for another build?"
            )
        name = (
            fields[3] if len(fields) > 3 and fields[3] else f"{chrom}:{start + 1}-{end}"
        )
        targets.append(BedTarget(name, contig, start, end))
    if unknown:
        raise UsageError(
            f"--by {path} names contigs the alignment lacks: {', '.join(sorted(unknown))}."
        )
    if not targets:
        raise UsageError(f"--by {path} has no targets.")
    for given, contig in sorted(renamed.items()):
        logging.info("--by: %s is %s in the alignment.", given, contig)
    return targets


def _merged(
    targets: Sequence[BedTarget], order: Mapping[str, int]
) -> list[tuple[str, int, int]]:
    spans = sorted((order[t.chrom], t.start, t.end, t.chrom) for t in targets)
    merged: list[tuple[str, int, int]] = []
    for _, start, end, chrom in spans:
        if merged and merged[-1][0] == chrom and start <= merged[-1][2]:
            merged[-1] = (chrom, merged[-1][1], max(merged[-1][2], end))
        else:
            merged.append((chrom, start, end))
    return merged


def _segments(
    targets: Sequence[BedTarget],
) -> dict[str, list[tuple[int, int, list[BedTarget]]]]:
    """Per contig, the stretches over which the same targets overlap."""
    by_contig: dict[str, list[BedTarget]] = {}
    for target in targets:
        by_contig.setdefault(target.chrom, []).append(target)
    result: dict[str, list[tuple[int, int, list[BedTarget]]]] = {}
    for chrom, items in by_contig.items():
        cuts = sorted({p for t in items for p in (t.start, t.end)})
        segments = []
        for start, end in zip(cuts, cuts[1:], strict=False):
            active = [t for t in items if t.start <= start and end <= t.end]
            if active:
                segments.append((start, end, active))
        result[chrom] = segments
    return result


def _count_depths(stream: Iterator[bytes], targets: Sequence[BedTarget]) -> None:
    """Fill each target's histogram from ``samtools depth -a`` rows; bases
    samtools leaves out count as depth 0."""
    segments = _segments(targets)
    counts: dict[tuple[str, int], dict[bytes, int]] = {}
    current: list[tuple[int, int, list[BedTarget]]] = []
    contig = b""
    index = 0
    histogram: dict[bytes, int] = {}
    for line in stream:
        name, pos, depth = line.split(b"\t")
        if name != contig:
            contig = name
            current = segments.get(name.decode(), [])
            index = 0
            histogram = {}
        position = int(pos) - 1
        while index < len(current) and position >= current[index][1]:
            index += 1
            histogram = {}
        if index == len(current) or position < current[index][0]:
            continue
        if not histogram:
            histogram = counts.setdefault((contig.decode(), index), {})
        histogram[depth] = histogram.get(depth, 0) + 1
    for chrom, items in segments.items():
        for index, (start, end, active) in enumerate(items):
            found = {int(d): n for d, n in counts.get((chrom, index), {}).items()}
            found[0] = found.get(0, 0) + (end - start) - sum(found.values())
            for target in active:
                for d, n in found.items():
                    target.histogram[d] = target.histogram.get(d, 0) + n


def _run_depth(
    input_path: str, reference: str | None, bed: Path, targets: Sequence[BedTarget]
) -> None:
    command = [
        "samtools", "depth", "-a", "-b", str(bed),
        *_cram_reference_opts(input_path, reference), input_path,
    ]  # fmt: skip
    with tempfile.TemporaryFile(dir=bed.parent) as stderr:
        process = popen(command, stdout=subprocess.PIPE, stderr=stderr)
        assert process.stdout is not None
        try:
            _count_depths(iter(process.stdout), targets)
            if process.wait():
                stderr.seek(0)
                detail = stderr.read().decode(errors="replace")[-4000:]
                raise WGSExtractError(f"samtools depth failed: {detail}")
        except ValueError as exc:
            raise WGSExtractError(f"Unexpected samtools depth row: {exc}") from exc
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                process.wait()


def _depth_sums(
    input_path: str,
    reference: str | None,
    bed: Path,
    merged: Sequence[tuple[str, int, int]],
    lengths: Mapping[str, int],
) -> tuple[int, int, int]:
    """On-target depth sum, off-target depth sum and off-target bases, from
    one ``samtools bedcov -j`` over the targets and the gaps between them
    (the mitochondrial contig is left out of off-target: its copy number
    isn't the nuclear genome's)."""
    regions: list[tuple[str, int, int, bool]] = []
    by_contig: dict[str, list[tuple[int, int]]] = {}
    for chrom, start, end in merged:
        by_contig.setdefault(chrom, []).append((start, end))
    for chrom, length in lengths.items():
        cursor = 0
        for start, end in by_contig.get(chrom, []):
            if cursor < start and chrom not in MITOCHONDRIA:
                regions.append((chrom, cursor, start, False))
            regions.append((chrom, start, end, True))
            cursor = end
        if cursor < length and chrom not in MITOCHONDRIA:
            regions.append((chrom, cursor, length, False))
    bed.write_text("".join(f"{c}\t{s}\t{e}\n" for c, s, e, _ in regions))
    output = run_command(
        ["samtools", "bedcov", "-j", str(bed),
         *_cram_reference_opts(input_path, reference), input_path],
        capture_output=True,
    )  # fmt: skip
    rows = output.stdout.splitlines()
    if len(rows) != len(regions):
        raise WGSExtractError(
            f"samtools bedcov returned {len(rows)} rows for {len(regions)} regions."
        )
    on = off = off_bases = 0
    for row, (_, start, end, target) in zip(rows, regions, strict=True):
        total = int(row.rsplit("\t", 1)[1])
        if target:
            on += total
        else:
            off += total
            off_bases += end - start
    return on, off, off_bases


def _columns() -> tuple[Column, ...]:
    return (
        Column("Name", "TEXT", "Target name (BED column 4), else contig:start-end."),
        Column("Contig", "TEXT", "Contig, as the alignment names it."),
        Column("Start", "INTEGER", "Start, 0-based (as in the BED)."),
        Column("End", "INTEGER", "End, exclusive."),
        Column("Length", "INTEGER", "Bases in the target."),
        Column("Mean depth", "REAL", "Mean depth over the target.", ".2f"),
        Column("Median depth", "REAL", "Median depth over the target.", ".1f"),
        *(
            Column(
                f"Fraction >={d}x",
                "REAL",
                f"Fraction of bases at depth {d} or more.",
                ".4f",
            )
            for d in DEPTHS
        ),
        Column(
            "Fraction >=min",
            "REAL",
            "Fraction of bases at --min-depth or more.",
            ".4f",
        ),
        Column("Low", "BOOLEAN", "Mean depth below --min-depth."),
    )


TARGET_TABLE = _columns()


def run_target_coverage(
    input_path: str,
    reference: str | None,
    lengths: Mapping[str, int],
    bed_path: Path,
    outdir: str,
    min_depth: int,
    table_format: str = "tsv",
    metadata: Mapping[str, str] | None = None,
) -> Path:
    targets = read_bed(bed_path, lengths)
    order = {name: i for i, name in enumerate(lengths)}
    merged = _merged(targets, order)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.info(
        "Calculating depth over %d targets (%s bases)...",
        len(targets),
        f"{sum(e - s for _, s, e in merged):,}",
    )
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".by-") as scratch:
        union = Path(scratch, "targets.bed")
        union.write_text("".join(f"{c}\t{s}\t{e}\n" for c, s, e in merged))
        _run_depth(input_path, reference, union, targets)
        logging.info("Summing on- and off-target depth (one pass over the file)...")
        on, off, off_bases = _depth_sums(
            input_path, reference, Path(scratch, "all.bed"), merged, lengths
        )

    on_bases = sum(e - s for _, s, e in merged)
    on_mean = on / on_bases
    off_mean = off / off_bases if off_bases else 0.0
    enrichment = on_mean / off_mean if off_mean else None
    low = [t for t in targets if t.mean < min_depth]
    covered = sum(t.bases_at_least(min_depth) for t in targets)
    target_bases = sum(t.length for t in targets)

    stem = f"{Path(input_path).name}_{bed_path.stem}"
    table = Path(table_path(Path(outdir, f"{stem}_target_coverage"), table_format))
    rows: list[tuple[Value, ...]] = [
        (
            t.name, t.chrom, t.start, t.end, t.length, t.mean, t.median,
            *(t.bases_at_least(d) / t.length for d in DEPTHS),
            t.bases_at_least(min_depth) / t.length,
            t.mean < min_depth,
        )
        for t in targets
    ]  # fmt: skip
    write_table(table, TARGET_TABLE, rows, table_format, metadata=metadata)

    summary = [
        f"Targets: {len(targets)} ({on_bases:,} bases after merging overlaps) "
        f"from {bed_path.name}",
        f"On-target mean depth: {on_mean:.2f}x",
        f"Off-target mean depth: {off_mean:.2f}x over {off_bases:,} bases "
        "(mitochondria left out)",
        "Enrichment (on/off-target mean): "
        + (
            f"{enrichment:.1f}x"
            if enrichment is not None
            else "n/a (no off-target depth)"
        ),
        f"On-target share of sequenced bases: {on / (on + off):.2%}"
        if on + off
        else "On-target share: n/a",
        f"Targeted bases at >={min_depth}x: {covered / target_bases:.2%}",
        f"Targets with mean depth below {min_depth}x: {len(low)} of {len(targets)}",
    ]
    summary_path = Path(outdir, f"{stem}_target_summary.txt")
    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")

    result = current_result()
    metrics: dict[str, Scalar] = {
        "targets": len(targets),
        "target_bases": on_bases,
        "on_target_mean_depth": round(on_mean, 3),
        "off_target_mean_depth": round(off_mean, 3),
        "enrichment": round(enrichment, 2) if enrichment is not None else None,
        "min_depth": min_depth,
        "targets_below_min_depth": len(low),
        "fraction_at_min_depth": round(covered / target_bases, 6),
    }
    result.add_output(table, "target_coverage", metrics={"rows": len(rows)})
    result.add_output(summary_path, "target_summary")
    result.add_metrics(**metrics)
    if low:
        warn(
            "targets_below_min_depth",
            f"{len(low)} of {len(targets)} targets have a mean depth below {min_depth}x.",
            targets=len(low),
            min_depth=min_depth,
        )

    human_output("\n".join(summary))
    if low:
        human_output(f"\nBelow {min_depth}x (mean, median, fraction >={min_depth}x):")
        for t in low:
            human_output(
                f"  {t.name}\t{t.chrom}:{t.start + 1}-{t.end}\t{t.mean:.1f}x\t"
                f"{t.median:g}x\t{t.bases_at_least(min_depth) / t.length:.1%}"
            )
    human_output(f"\nPer-target table: {table}\nSummary: {summary_path}")
    return table
