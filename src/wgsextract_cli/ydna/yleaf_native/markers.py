# SPDX-License-Identifier: GPL-3.0-or-later
# Derived from genid/Yleaf v4.1.6, yleaf/Yleaf.py: run_vcf,
# _infer_from_reference, extract_haplogroups, get_frequencies, _find_digit.
# Copyright Diego Montiel Gonzalez and Bram van Wersch, Erasmus MC.
# Modified 2026-11-01 by WGS Extract contributors: typed records instead of pandas,
# bcftools/samtools argv integration and bounded chrY pileup.
# Upstream tag/commit recorded in provenance.json.
"""Yleaf marker matching, base counting, and sequential QC filters."""

import csv
import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .tree import Tree

MARKER_HEADER = (
    "chr",
    "pos",
    "marker_name",
    "haplogroup",
    "mutation",
    "anc",
    "der",
    "reads",
    "called_perc",
    "called_base",
    "state",
)


@dataclass(frozen=True)
class Marker:
    chromosome: str
    name: str
    haplogroup: str
    pos: int
    mutation: str
    ancestral: str
    derived: str


@dataclass(frozen=True)
class Call:
    base: str
    reads: int
    percentage: float


@dataclass(frozen=True)
class Observation:
    marker: Marker
    call: Call
    state: str
    description: str = ""

    def row(self) -> list[str]:
        marker, call = self.marker, self.call
        zero = self.description == "Position with zero reads"
        return list(
            map(
                str,
                [
                    marker.chromosome,
                    marker.pos,
                    marker.name,
                    marker.haplogroup,
                    marker.mutation,
                    marker.ancestral,
                    marker.derived,
                    call.reads,
                    "NA" if zero else call.percentage,
                    "NA" if zero else call.base,
                    self.state,
                ],
            )
        )


def read_markers(path: Path) -> list[Marker]:
    seen: set[tuple[str, int]] = set()
    markers = []
    with path.open(encoding="utf-8") as stream:
        for chromosome, name, hg, pos, mutation, anc, der in csv.reader(
            stream, delimiter="\t"
        ):
            key = hg, int(pos)
            if key in seen:
                continue
            seen.add(key)
            markers.append(Marker(chromosome, name, hg, int(pos), mutation, anc, der))
    return markers


def gt_call(ref: str, alts: str, genotype: str) -> Call | None:
    allele = genotype.replace("|", "/").split("/")[0]
    if allele in (".", ""):
        return None
    try:
        index = int(allele)
    except ValueError:
        return None
    bases = [ref, *alts.split(",")]
    if not 0 <= index < len(bases):
        return None
    return Call(bases[index], 1, 100.0)


def ad_call(ref: str, alts: str, depths: str) -> Call:
    counts = [int(value) if value != "." else 0 for value in depths.split(",")]
    ref_reads = counts[0]
    alt_reads = dict(zip(alts.split(","), counts[1:], strict=False))
    highest = max(alt_reads.values(), default=0)
    highest_base = (
        max(alt_reads, key=lambda base: alt_reads[base]) if alt_reads else "NA"
    )
    # Upstream compares ref only with the highest-depth alternate, *not* all AD.
    total = ref_reads + highest
    ref_perc = round(ref_reads / total * 100, 1) if total else 0
    alt_perc = round(highest / total * 100, 1) if total else 0
    if ref_perc >= alt_perc:
        return Call(ref, ref_reads, ref_perc)
    return Call(highest_base, highest, alt_perc)


def get_frequencies(sequence: str) -> dict[str, int]:
    sequence = sequence.upper()
    if set("+-^$").isdisjoint(sequence):
        frequency = Counter(sequence)
        return {
            "A": frequency.get("A", 0),
            "T": frequency.get("T", 0),
            "G": frequency.get("G", 0),
            "C": frequency.get("C", 0),
            "-": frequency.get("*", 0),
            "+": 0,
        }
    counts = dict.fromkeys(("A", "T", "G", "C", "-", "+", "*"), 0)
    index = 0
    while index < len(sequence):
        char = sequence[index]
        if char in ("-", "+"):
            counts[char] += 1
            index += 1
            start = index
            while index < len(sequence) and sequence[index].isdigit():
                index += 1
            index += int(sequence[start:index])
        elif char == "^":
            index += 2
        elif char in counts:
            counts[char] += 1
            index += 1
        else:
            index += 1
    counts["-"] += counts.pop("*")
    return counts


def pileup_call(ref: str, reads: int, sequence: str) -> Call:
    sequence = sequence.replace(",", ref[0]).replace(".", ref[0])
    counts = get_frequencies(sequence)
    bases = ("A", "T", "G", "C")
    base = max(bases, key=lambda value: counts[value])
    total = sum(counts[value] for value in bases)
    # v4.1.6 rounds to one decimal and then truncates to an integer for BAM/CRAM.
    percentage = int(round(counts[base] / total * 100, 1)) if total else 0
    return Call(base, reads, percentage)


def match_markers(
    markers: Iterable[Marker],
    calls: dict[int, Call],
    *,
    min_reads: int = 10,
    majority: int = 90,
    reference_sequence: str | None = None,
) -> tuple[list[Observation], list[Observation]]:
    passing, failed = [], []
    for marker in sorted(markers, key=lambda value: value.pos):
        call = calls.get(marker.pos)
        if call is None:
            if reference_sequence and 0 < marker.pos <= len(reference_sequence):
                base = reference_sequence[marker.pos - 1]
                if base in ("A", "T", "G", "C") and base in (
                    marker.ancestral,
                    marker.derived,
                ):
                    state = "A" if base == marker.ancestral else "D"
                    passing.append(Observation(marker, Call(base, 0, 100.0), state))
            continue
        state = (
            "A"
            if call.base == marker.ancestral
            else "D"
            if call.base == marker.derived
            else "NA"
        )
        description = ""
        if call.reads == 0:
            state = "NA"
            description = "Position with zero reads"
        elif state == "NA":
            description = "Discordant genotype"
        elif call.reads < min_reads:
            description = "Below read threshold"
        elif not math.isfinite(call.percentage) or call.percentage < majority:
            description = "Below base majority"
        observation = Observation(marker, call, state, description)
        if description:
            failed.append(observation)
        else:
            passing.append(observation)
    return passing, failed


def write_marker_tables(
    folder: Path,
    sample: str,
    tree: Tree,
    passing: list[Observation],
    failed: list[Observation],
) -> list[Observation]:
    grouped: dict[str, list[Observation]] = {}
    for observation in passing:
        grouped.setdefault(observation.marker.haplogroup, []).append(observation)
    ordered = []
    with (folder / f"{sample}.out").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow((*MARKER_HEADER, "depth"))
        for name, node in tree.node_mapping.items():
            for observation in grouped.get(name, []):
                writer.writerow([*observation.row(), node.depth])
                ordered.append(observation)
    with (folder / f"{sample}.fmf").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow((*MARKER_HEADER, "Description"))
        for observation in failed:
            writer.writerow([*observation.row(), observation.description])
    return ordered
