# SPDX-License-Identifier: GPL-3.0-or-later
# Derived from genid/Yleaf v4.1.6, yleaf/predict_haplogroup.py
# (commit recorded in provenance.json).
# Copyright Bram van Wersch, Erasmus MC Department of Genetic Identification.
# Modified 2026-11-01 by WGS Extract contributors: typed API, per-call state
# instead of process globals, deterministic marker presentation.
"""Yleaf's deepest-passing-node prediction and QC1/QC2/QC3 scoring."""

import csv
from collections.abc import Iterable
from math import prod
from pathlib import Path
from typing import NamedTuple

from .tree import Node, Tree

DEFAULT_MIN_SCORE = 0.95
TREE_FILES = {
    "yfull": "tree.json",
    "yfull_v10": "yfull_v10_tree.json",
    "ftdna": "ftdna_tree.json",
    "isogg": "isogg_tree.json",
}
MAJOR_TABLES = {
    "yfull": "major_tables",
    "yfull_v10": "yfull_v10_major_tables",
    "ftdna": "ftdna_major_tables",
    "isogg": "isogg_major_tables",
}
MAJOR_TREE_LIST = (
    "A00 A00a A00b A00c A0-T A1 A1b A1b1 BT CT CF F F4 F2 F3 GHIJK G HIJK H "
    "IJK IJ I I2 I1 J J1 J2 K K2 K2d K2c K2b P R R2 R1 R1b R1a Q K2b1 S M "
    "NO O N LT L T F1 C DE D E B A1a A0"
).split()


class HgMarkersLinker:
    DERIVED = "D"
    ANCESTRAL = "A"
    UNDEFINED = "N"

    def __init__(self) -> None:
        self._ancestral_markers: set[str] = set()
        self._derived_markers: set[str] = set()
        self._d = 0
        self._a = 0

    def add(self, marker_name: str, state: str) -> None:
        if state == self.DERIVED:
            self._derived_markers.add(marker_name)
            self._d += 1
        else:
            self._ancestral_markers.add(marker_name)
            self._a += 1

    def get_derived_markers(self) -> set[str]:
        return self._derived_markers

    @property
    def nr_total(self) -> int:
        return self._d + self._a

    @property
    def nr_derived(self) -> int:
        return self._d

    @property
    def nr_ancestral(self) -> int:
        return self._a

    def get_state(self) -> str:
        total = self._d + self._a
        if total == 0:
            return self.UNDEFINED
        if self._d / total >= 0.6:
            return self.DERIVED
        if self._a / total >= 0.6:
            return self.ANCESTRAL
        return self.UNDEFINED


Haplotypes = dict[str, HgMarkersLinker]


def link_markers(rows: Iterable[tuple[str, str, str, str]]) -> Haplotypes:
    """Exclude approximate placements and recurrent positions from *all* QC."""
    parsed = []
    pos_haplogroups: dict[str, set[str]] = {}
    for pos, marker, haplogroup, state in rows:
        if haplogroup.endswith("~"):
            continue
        parsed.append((pos, marker, haplogroup, state))
        pos_haplogroups.setdefault(pos, set()).add(haplogroup)
    recurrent_positions = {p for p, hgs in pos_haplogroups.items() if len(hgs) >= 2}
    haplotypes: Haplotypes = {}
    for pos, marker, haplogroup, state in parsed:
        if pos in recurrent_positions:
            continue
        haplotypes.setdefault(haplogroup, HgMarkersLinker()).add(marker, state)
    return haplotypes


def read_yleaf_out_file(file: Path | str) -> Haplotypes:
    with open(file, encoding="utf-8") as stream:
        rows = csv.DictReader(stream, delimiter="\t")
        return link_markers(
            (row["pos"], row["marker_name"], row["haplogroup"], row["state"])
            for row in rows
        )


class Scores(NamedTuple):
    haplogroup: str
    ancestral_children: list[str]
    qc1: float | str
    qc2: float | str
    qc3: float | str
    total: float | str
    depth: int | str


class Predictor:
    """Isolate upstream globals/caches so samples and trees cannot leak state."""

    def __init__(self, folder: Path, active_tree: str = "yfull") -> None:
        self.folder = folder
        self.active_tree = active_tree
        self.tree = Tree(folder / TREE_FILES[active_tree])
        self.backbone_groups: set[str] = set()
        self.main_haplo_groups: set[str] = set()
        self.qc1_score_cache: dict[str, float] = {}
        with (folder / MAJOR_TABLES[active_tree] / "Intermediates.txt").open(
            encoding="utf-8"
        ) as stream:
            for line in stream:
                if "~" in line:
                    continue
                name = line.strip()
                if name:
                    self.backbone_groups.add(name)
                    self.main_haplo_groups.add(name)
        if active_tree in ("yfull", "yfull_v10"):
            self.main_haplo_groups.update(MAJOR_TREE_LIST)

    def predict(
        self, haplotype_dict: Haplotypes, threshold: float = DEFAULT_MIN_SCORE
    ) -> Scores:
        self.qc1_score_cache.clear()
        tree = self.tree
        sorted_depth_haplotypes = sorted(
            haplotype_dict, key=lambda k: tree.get(k).depth, reverse=True
        )
        covered_nodes: set[str] = set()
        best_score = Scores("NA", [], "NA", "NA", "NA", "NA", "NA")
        for haplotype_name in sorted_depth_haplotypes:
            node = tree.get(haplotype_name)
            if node.name in covered_nodes:
                continue
            h = haplotype_dict[haplotype_name]
            qc2_score = h.nr_derived / h.nr_total if h.nr_total else 0
            if qc2_score < threshold:
                continue
            node_name = node.name
            node_first_char = node_name[0]
            parent = node.parent
            path = [node_name]
            qc3_matching, qc3_total = 0, 0
            while parent is not None:
                pname = parent.name
                path.append(pname)
                if (
                    pname in haplotype_dict
                    and node_first_char in pname
                    and pname != node_first_char
                ):
                    state = haplotype_dict[pname].get_state()
                    if state == HgMarkersLinker.DERIVED:
                        qc3_matching += 1
                    if state != HgMarkersLinker.UNDEFINED:
                        qc3_total += 1
                parent = parent.parent
            qc1_score = self.get_qc1_score(path, haplotype_dict)
            if qc1_score < threshold:
                continue
            if qc3_total == 0:
                qc3_score = 0 if self.active_tree == "ftdna" else 1.0
            else:
                qc3_score = qc3_matching / qc3_total
                if qc3_score < threshold:
                    continue
            total_score = prod([qc1_score, qc2_score, qc3_score])
            if total_score >= threshold:
                children = get_ancestral_children(node, haplotype_dict, tree)
                best_score = Scores(
                    node.name,
                    children,
                    qc1_score,
                    qc2_score,
                    qc3_score,
                    total_score,
                    node.depth,
                )
                break
            best_score = Scores(
                "NA", [], qc1_score, qc2_score, qc3_score, total_score, "NA"
            )
            # Upstream adds strings here but checks Node objects above. Preserve
            # its effective behavior: failed descendants do not suppress parents.
        return best_score

    def get_qc1_score(self, path: list[str], haplotype_dict: Haplotypes) -> float:
        most_specific_backbone = next(
            (value for value in path if value in self.main_haplo_groups), None
        )
        if most_specific_backbone is None:
            return 0
        if most_specific_backbone in self.qc1_score_cache:
            return self.qc1_score_cache[most_specific_backbone]
        # v4.1.6 intentionally uses v14 QC1 expectations even for yfull_v10.
        tables = MAJOR_TABLES[self.active_tree]
        if self.active_tree == "yfull_v10":
            tables = "major_tables"
        expected_states: dict[str, set[str]] = {}
        with (self.folder / tables / f"{most_specific_backbone}_int.txt").open(
            encoding="utf-8"
        ) as stream:
            for line in stream:
                if line == "":
                    continue
                name, state = line.strip().split("\t")
                expected_states[name] = set(state.split("/"))
        intermediate_states = {
            value: haplotype_dict[value]
            for value in self.backbone_groups
            if value in haplotype_dict
        }
        matching, total = 0, 0
        if self.active_tree == "ftdna":
            for name, expected in expected_states.items():
                total += 1
                if name not in intermediate_states:
                    matching += 1
                else:
                    state = intermediate_states[name].get_state()
                    if state == HgMarkersLinker.UNDEFINED or state in expected:
                        matching += 1
        else:
            for name, linker in intermediate_states.items():
                possible = expected_states.get(name)
                if possible is None:
                    continue
                state = linker.get_state()
                if state in possible:
                    matching += 1
                if state != HgMarkersLinker.UNDEFINED:
                    total += 1
        if total == 0:
            return 1.0
        score = matching / total
        self.qc1_score_cache[most_specific_backbone] = score
        return score


def get_ancestral_children(
    node: Node, haplotype_dict: Haplotypes, tree: Tree
) -> list[str]:
    ancestral_children = []
    to_cover = [node]
    while to_cover:
        node = to_cover.pop()
        for name in node.children:
            if (
                name in haplotype_dict
                and haplotype_dict[name].get_state() == HgMarkersLinker.ANCESTRAL
            ):
                ancestral_children.append(name)
                continue
            to_cover.append(tree.get(name))
    return ancestral_children


def prediction_row(
    sample: str, haplotypes: Haplotypes, scores: Scores, total_reads: str
) -> list[str]:
    hg, children, qc1, qc2, qc3, total, _ = scores
    markers: list[str] = []
    if hg != "NA":
        markers = sorted(haplotypes[hg].get_derived_markers())
        if len(markers) > 2:
            markers = markers[:2] + ["etc."]
        if children:
            hg = f"{hg}*(x{','.join(children)})"
    valid_markers = sum(linker.nr_total for linker in haplotypes.values())
    return list(
        map(
            str,
            [
                sample,
                hg,
                ";".join(markers),
                total_reads,
                valid_markers,
                total,
                qc1,
                qc2,
                qc3,
            ],
        )
    )


PREDICTION_HEADER = (
    "Sample_name",
    "Hg",
    "Hg_marker",
    "Total_reads",
    "Valid_markers",
    "QC-score",
    "QC-1",
    "QC-2",
    "QC-3",
)
