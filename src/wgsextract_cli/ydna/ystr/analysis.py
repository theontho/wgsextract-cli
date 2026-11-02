"""Call Y-STR repeat counts from an indexed BAM/CRAM with the bundled panel."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from wgsextract_cli.core.utils import WGSExtractError, run_command

from .counter import TractReads, collect
from .panel import Marker, Tract, markers, provenance, tracts

Y_NAMES = ("chrY", "Y")
# Unmapped, secondary, QC-fail, duplicate and supplementary reads.
EXCLUDE_FLAGS = "0xF04"
# Reads of a multicopy marker map to any copy with low MAPQ; an allele one
# repeat from a stronger one needs this share of their length-corrected
# spanning reads, or it is taken for stutter (usually under 15%).
SECOND_ALLELE_FRACTION = 0.2
# Reads with one flank needed to show an allele longer than any spanning read.
LONGER_ALLELE_READS = 2
# Reads needed to call a duplicated marker's copies identical: a read samples
# either copy, so k reads all miss one copy with probability about 2 * 0.5**k.
HOMOZYGOUS_READS = 6

STATUS_TEXT = {
    "called": "",
    "not_callable": "not callable from short reads",
    "not_in_build": "marker has no unique location in this reference build",
    "no_reads": "no reads at the marker",
    "too_long": "repeat is longer than the reads can span",
    "no_spanning": "no read spans the repeat and both flanks",
    "low_support": "too few reads support the most common length",
    "conflicting": "reads disagree on the length",
}


@dataclass
class Call:
    marker: Marker
    status: str
    value: str = ""
    spanning: int = 0
    support: int = 0
    # Supporting reads per value, one table per measured part.
    alleles: list[dict[str, int]] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return self.support / self.spanning if self.spanning else 0.0


def format_count(total_bp: int, period: int) -> str:
    """A repeat count; a partial repeat is written ISFG-style as ``n.r``."""
    units, rest = divmod(total_bp, period)
    return f"{units}.{rest}" if rest else str(units)


def nist_value(call: Call) -> str:
    if not call.value or not call.marker.ftdna_minus_nist:
        return call.value
    units, _, rest = call.value.partition(".")
    nist = str(int(units) - call.marker.ftdna_minus_nist)
    return f"{nist}.{rest}" if rest else nist


def _part_status(
    reads: TractReads,
    tract: Tract,
    period: int,
    min_reads: int,
    min_fraction: float,
) -> str:
    if not reads.spanning:
        if not reads.overlapping:
            return "no_reads"
        span = tract.end - tract.start + 1 + len(tract.left) + len(tract.right)
        return "too_long" if span > reads.longest_read else "no_spanning"
    mode = reads.mode()
    if mode is None:
        return "conflicting"
    if mode[1] < min_reads:
        return "low_support"
    if mode[1] / reads.spanning < min_fraction:
        return "conflicting"
    # Spanning reads favour short alleles; reads with one flank may show the
    # allele is longer than the spanning reads' (e.g. those are artefacts).
    longer = reads.longer_than(mode[0] + period)
    if longer >= max(LONGER_ALLELE_READS, mode[1]):
        return "conflicting"
    return "called"


def call_single(
    marker: Marker,
    parts: Sequence[Tract],
    reads: dict[str, TractReads],
    min_reads: int,
    min_fraction: float,
) -> Call:
    """A one-copy marker: the modal length of each part, summed."""
    assert marker.hg38_value is not None
    total_bp = marker.hg38_value * marker.period
    spanning: list[int] = []
    support: list[int] = []
    modes: list[int] = []
    for tract in parts:
        part = reads[key(tract)]
        status = _part_status(part, tract, marker.period, min_reads, min_fraction)
        mode = part.mode()
        spanning.append(part.spanning)
        support.append(mode[1] if mode else 0)
        if status != "called":
            return Call(marker, status, spanning=min(spanning), support=min(support))
        assert mode is not None
        modes.append(mode[0])
        total_bp += mode[0] - tract.hg38_length
    # Each part's reads as marker values, the other parts at their modal length.
    distributions = []
    for tract, mode_len in zip(parts, modes, strict=True):
        counts: Counter[str] = Counter()
        for length, n in reads[key(tract)].lengths.items():
            counts[format_count(total_bp + length - mode_len, marker.period)] += n
        distributions.append(dict(counts))
    return Call(
        marker,
        "called",
        format_count(total_bp, marker.period),
        min(spanning),
        min(support),
        distributions,
    )


def call_multicopy(
    group: Sequence[Marker],
    parts: Sequence[Tract],
    reads: dict[str, TractReads],
    min_reads: int,
) -> list[Call]:
    """Copies of a duplicated marker, pooled and reported in ascending order.

    Each read's length is converted with the GRCh38 count of the copy it
    aligned to, so a read misplaced on the other copy still counts correctly
    when the copies' tracts have the same structure.
    """
    period = group[0].period
    if any(t.copy_value is None for t in parts):
        return [Call(m, "not_callable") for m in group]
    pooled: Counter[int] = Counter()
    observed: list[tuple[int, int, int]] = []  # (value in bases, read tract, reads)
    bounds: Counter[int] = Counter()
    overlapping = longest = 0
    for tract in parts:
        part = reads[key(tract)]
        overlapping += part.overlapping
        longest = max(longest, part.longest_read)
        for length, n in part.lengths.items():
            assert tract.copy_value is not None
            bp = tract.copy_value * period + length - tract.hg38_length
            pooled[bp] += n
            observed.append((bp, length + len(tract.left) + len(tract.right), n))
        for bound, n in part.bounds.items():
            assert tract.copy_value is not None
            bounds[tract.copy_value * period + bound - tract.hg38_length] += n
    spanning = sum(pooled.values())
    # Fewer reads span a longer allele: weigh each read by the inverse of the
    # number of read placements that span it, so a long second copy isn't
    # mistaken for stutter.
    weight: dict[int, float] = {}
    for bp, span, n in observed:
        weight[bp] = weight.get(bp, 0.0) + n / max(1, longest - span + 1)
    total_weight = sum(weight.values())
    ranked = pooled.most_common()
    status = "called"
    if not spanning:
        span = max(t.end - t.start + 1 + len(t.left) + len(t.right) for t in parts)
        status = (
            "no_reads"
            if not overlapping
            else "too_long"
            if span > longest
            else "no_spanning"
        )
    elif ranked[0][1] < min_reads:
        status = "low_support"

    def stutter(bp: int) -> bool:
        """One repeat from a stronger allele, and too weak to be a copy."""
        return weight[bp] < SECOND_ALLELE_FRACTION * total_weight and any(
            abs(bp - other) == period and weight[other] > weight[bp] for other in weight
        )

    alleles = [bp for bp, n in ranked if n >= min(2, min_reads) and not stutter(bp)]
    if status == "called" and len(alleles) > len(group):
        status = "conflicting"
    table = [{format_count(bp, period): n for bp, n in sorted(pooled.items())}]
    if status != "called":
        return [Call(m, status, spanning=spanning, alleles=table) for m in group]
    values = sorted(alleles)
    if len(values) < len(group):
        # Too few distinct alleles: the copies may share one, or a longer copy
        # may have too few spanning reads (fewer reads span a longer allele).
        # Reads with one flank, or lone spanning reads, longer than the
        # observed alleles show the latter.
        top = values[-1] + period
        longer = sum(n for bp, n in bounds.items() if bp > top)
        lone = sum(n for bp, n in pooled.items() if bp > top)
        if longer + lone:
            status = "too_long" if longer >= LONGER_ALLELE_READS else "low_support"
            first = Call(
                group[0],
                "called",
                format_count(values[0], period),
                spanning,
                pooled[values[0]],
                table,
            )
            calls = [first] + [
                Call(m, "called", format_count(bp, period), spanning, pooled[bp], table)
                for m, bp in zip(group[1 : len(values)], values[1:], strict=True)
            ]
            return calls + [
                Call(m, status, spanning=spanning, alleles=table)
                for m in group[len(values) :]
            ]
        if pooled[values[-1]] < max(min_reads, HOMOZYGOUS_READS):
            # Without enough reads the copies can't be called identical, nor
            # the one allele seen be assigned to a copy.
            return [
                Call(m, "low_support", spanning=spanning, alleles=table) for m in group
            ]
    values += [values[-1]] * (len(group) - len(values))
    return [
        Call(m, "called", format_count(bp, period), spanning, pooled[bp], table)
        for m, bp in zip(group, values, strict=True)
    ]


def key(tract: Tract) -> str:
    return f"{tract.group}#{tract.part}"


def read_alignments(
    path: Path,
    contig: str,
    panel: dict[str, tuple[Tract, ...]],
    work: Path,
    reference: Path | None,
) -> list[tuple[str, int, str, int]]:
    """Reads overlapping the panel's tracts: (contig, start, sequence, MAPQ)."""
    bed = work / "ystr.bed"
    rows = sorted({(t.start - 1, t.end) for parts in panel.values() for t in parts})
    bed.write_text(
        "".join(f"{contig}\t{s}\t{e}\n" for s, e in rows),
        encoding="utf-8",
        newline="\n",
    )
    command = ["samtools", "view", "-F", EXCLUDE_FLAGS, "-M", "-L", str(bed)]
    if reference and path.suffix.lower() == ".cram":
        command += ["-T", str(reference)]
    command.append(str(path))
    out = run_command(command, capture_output=True).stdout
    reads = []
    for line in out.splitlines():
        fields = line.split("\t", 10)
        if len(fields) < 10 or fields[9] == "*":
            continue
        reads.append((fields[2], int(fields[3]), fields[9], int(fields[4])))
    return reads


def y_contig(path: Path) -> str:
    """The Y chromosome's name in the alignment header."""
    header = run_command(["samtools", "view", "-H", str(path)], capture_output=True)
    names = {
        field[3:]
        for line in header.stdout.splitlines()
        if line.startswith("@SQ")
        for field in line.split("\t")
        if field.startswith("SN:")
    }
    for name in Y_NAMES:
        if name in names:
            return name
    raise WGSExtractError(f"{path} has no Y chromosome (chrY or Y).")


def call_markers(
    reads: Iterable[tuple[str, int, str, int]],
    contig: str,
    build: str,
    selected: Sequence[Marker],
    *,
    min_reads: int,
    min_fraction: float,
    min_mapq: int,
) -> list[Call]:
    panel = tracts(build)
    unique: dict[str, tuple[str, int, int, str, str]] = {}
    shared: dict[str, tuple[str, int, int, str, str]] = {}
    for m in selected:
        for t in panel.get(m.group, ()):
            target = shared if m.kind == "multicopy" else unique
            target[key(t)] = (contig, t.start, t.end, t.left, t.right)
    reads = list(reads)
    measured = collect(
        ((c, p, q) for c, p, q, mapq in reads if mapq >= min_mapq), unique
    )
    measured |= collect(((c, p, q) for c, p, q, _mapq in reads), shared)

    calls: list[Call] = []
    done: set[str] = set()
    for m in selected:
        if m.group in done:
            continue
        if m.kind == "none":
            calls.append(Call(m, "not_callable"))
            continue
        parts = panel.get(m.group)
        if m.kind == "multicopy":
            done.add(m.group)
            group = [g for g in selected if g.group == m.group]
            if not parts:
                calls += [Call(g, "not_in_build") for g in group]
            else:
                calls += call_multicopy(group, parts, measured, min_reads)
        elif not parts or m.hg38_value is None:
            calls.append(Call(m, "not_in_build" if not parts else "not_callable"))
        else:
            calls.append(call_single(m, parts, measured, min_reads, min_fraction))
    return calls


def family(marker: Marker) -> str:
    """FamilyTreeDNA's column for a marker: copies (DYS385a, b) share one."""
    return marker.name[:-1] if marker.name[-1].islower() else marker.name


def write_ftdna_csv(calls: Sequence[Call], path: Path) -> None:
    """One row in FamilyTreeDNA's Y-111 column order; copies joined by '-'."""
    columns: dict[str, list[Call]] = {}
    for call in sorted(
        (c for c in calls if c.marker.ftdna_order is not None),
        key=lambda c: c.marker.ftdna_order or 0,
    ):
        columns.setdefault(family(call.marker), []).append(call)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(columns)
        writer.writerow(
            "-".join(c.value for c in group) if all(c.value for c in group) else ""
            for group in columns.values()
        )


TSV_HEADER = (
    "marker",
    "ftdna_order",
    "ftdna_panel",
    "kits",
    "value",
    "nist_value",
    "status",
    "reason",
    "spanning_reads",
    "supporting_reads",
    "support_fraction",
    "read_alleles",
    "value_convention",
)


def write_tsv(calls: Sequence[Call], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(TSV_HEADER)
        for c in calls:
            m = c.marker
            writer.writerow(
                (
                    m.name,
                    m.ftdna_order or "",
                    m.ftdna_panel,
                    ",".join(m.kits),
                    c.value,
                    nist_value(c),
                    c.status,
                    m.note
                    if c.status == "not_callable" and m.note
                    else STATUS_TEXT[c.status],
                    c.spanning,
                    c.support,
                    f"{c.fraction:.2f}" if c.spanning else "",
                    "|".join(
                        ",".join(f"{v}:{n}" for v, n in a.items()) for a in c.alleles
                    ),
                    m.value_source,
                )
            )


def analyze(
    input_path: Path,
    output: Path,
    *,
    build: str,
    selected: Sequence[Marker] | None = None,
    reference: Path | None = None,
    min_reads: int = 2,
    min_fraction: float = 0.6,
    min_mapq: int = 20,
) -> tuple[list[Call], Path]:
    """Write <sample>.ystr.tsv, <sample>.ftdna.csv and run.json under ``output``."""
    if min_reads < 1 or not 0.5 <= min_fraction <= 1 or min_mapq < 0:
        raise WGSExtractError("Invalid Y-STR read, fraction or MAPQ threshold.")
    if input_path.suffix.lower() not in (".bam", ".cram"):
        raise WGSExtractError("Y-STR calling needs an indexed BAM or CRAM.")
    if input_path.suffix.lower() == ".cram" and not reference:
        raise WGSExtractError("A reference FASTA (--ref) is required to decode CRAM.")
    selected = tuple(selected or markers())
    output.mkdir(parents=True, exist_ok=True)
    sample = input_path.name.rsplit(".", 1)[0]
    contig = y_contig(input_path)
    logging.info(
        "Y-STR panel: %d markers, build %s, contig %s", len(selected), build, contig
    )
    reads = read_alignments(input_path, contig, tracts(build), output, reference)
    (output / "ystr.bed").unlink(missing_ok=True)
    calls = call_markers(
        reads,
        contig,
        build,
        selected,
        min_reads=min_reads,
        min_fraction=min_fraction,
        min_mapq=min_mapq,
    )
    table = output / f"{sample}.ystr.tsv"
    write_tsv(calls, table)
    write_ftdna_csv(calls, output / f"{sample}.ftdna.csv")
    meta = provenance()
    (output / "run.json").write_text(
        json.dumps(
            {
                "algorithm": "flank-anchored Y-STR counting (wgsextract)",
                "input": input_path.name,
                "build": build,
                "markers": len(selected),
                "called": sum(c.status == "called" for c in calls),
                "min_reads": min_reads,
                "min_fraction": min_fraction,
                "min_mapq": min_mapq,
                "panel_sources": {k: v.get("url") for k, v in meta["sources"].items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return calls, table
