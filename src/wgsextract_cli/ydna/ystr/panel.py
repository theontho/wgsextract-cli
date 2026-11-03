"""The bundled Y-STR marker panel (markers.tsv, loci.tsv; see README.md)."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent
BUILDS = ("hg38", "hg19", "t2t")
# FamilyTreeDNA's cumulative Y-DNA STR panels.
FTDNA_PANELS = ("Y-12", "Y-25", "Y-37", "Y-67", "Y-111")
PANELS = (*FTDNA_PANELS, "PPY23", "YfilerPlus", "all")


@dataclass(frozen=True)
class Marker:
    name: str
    group: str
    ftdna_order: int | None
    ftdna_panel: str
    kits: tuple[str, ...]
    period: int
    kind: str  # single, sum, multicopy or none
    hg38_value: int | None
    value_source: str
    ftdna_minus_nist: int
    note: str

    @property
    def value_verified(self) -> bool:
        return not self.value_source.startswith("unverified")


@dataclass(frozen=True)
class Tract:
    group: str
    part: int
    start: int  # 1-based, inclusive
    end: int
    left: str
    right: str
    hg38_length: int
    copy_value: int | None  # multicopy markers: this copy's GRCh38 count


def _int(text: str) -> int | None:
    return int(text) if text else None


@cache
def markers() -> tuple[Marker, ...]:
    with open(DATA / "markers.tsv", encoding="utf-8", newline="") as f:
        return tuple(
            Marker(
                name=row["marker"],
                group=row["group"],
                ftdna_order=_int(row["ftdna_order"]),
                ftdna_panel=row["ftdna_panel"],
                kits=tuple(k for k in row["kits"].split(",") if k),
                period=int(row["period"]),
                kind=row["kind"],
                hg38_value=_int(row["hg38_value"]),
                value_source=row["value_source"],
                ftdna_minus_nist=int(row["ftdna_minus_nist"]),
                note=row["note"],
            )
            for row in csv.DictReader(f, delimiter="\t")
        )


@cache
def tracts(build: str) -> dict[str, tuple[Tract, ...]]:
    """Each marker group's tracts in ``build``; groups missing there are absent."""
    if build not in BUILDS:
        raise ValueError(f"Unknown Y-STR panel build: {build}")
    found: dict[str, list[Tract]] = {}
    with open(DATA / "loci.tsv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["build"] == build:
                found.setdefault(row["group"], []).append(
                    Tract(
                        group=row["group"],
                        part=int(row["part"]),
                        start=int(row["start"]),
                        end=int(row["end"]),
                        left=row["left"],
                        right=row["right"],
                        hg38_length=int(row["hg38_length"]),
                        copy_value=_int(row["copy_value"]),
                    )
                )
    # A summed marker needs every part; a copy of a multicopy marker may be
    # missing from an assembly, but the parts tell which.
    expected: dict[str, int] = {}
    with open(DATA / "loci.tsv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["build"] == "hg38":
                expected[row["group"]] = expected.get(row["group"], 0) + 1
    by_kind = {m.group: m.kind for m in markers()}
    return {
        group: tuple(parts)
        for group, parts in found.items()
        if by_kind[group] == "multicopy" or len(parts) == expected[group]
    }


def select(panel: str) -> tuple[Marker, ...]:
    """Markers in a FamilyTreeDNA panel (cumulative), a kit, or all of them."""
    if panel == "all":
        return markers()
    if panel in FTDNA_PANELS:
        wanted = FTDNA_PANELS[: FTDNA_PANELS.index(panel) + 1]
        return tuple(m for m in markers() if m.ftdna_panel in wanted)
    if panel in PANELS:
        return tuple(m for m in markers() if panel in m.kits)
    raise ValueError(f"Unknown Y-STR panel: {panel}")


@cache
def provenance() -> dict[str, Any]:
    with open(DATA / "provenance.json", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data
