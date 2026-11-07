"""Small synthetic marker/tree tables shared by native lineage tests."""

import json
from pathlib import Path

import pytest

from wgsextract_cli.ydna.yleaf_native import analysis, trees

MARKERS = (
    "Y\tM-R\tR\t100\tA>G\tA\tG\n"
    "Y\tM-R1\tR1\t110\tA>G\tA\tG\n"
    "Y\tM-R1b\tR1b\t120\tA>G\tA\tG\n"
    "Y\tM-leaf\tR1b1\t130\tA>G\tA\tG\n"
    "Y\tM-child\tR1b2\t140\tA>G\tA\tG\n"
    "Y\tM-C\tC\t150\tA>G\tA\tG\n"
    "Y\tM-approx\tC~\t151\tA>G\tA\tG\n"
    "Y\tM-recur-R\tR1b1\t160\tA>G\tA\tG\n"
    "Y\tM-recur-C\tC\t160\tA>G\tA\tG\n"
    "Y\tM-duplicate\tR1b\t120\tA>G\tA\tG\n"
)


def make_data(root: Path) -> Path:
    tree = {
        'ROOT (Y-Chromosome "Adam")': ["R", "C", "C~"],
        "R": ["R1"],
        "R1": ["R1b"],
        "R1b": ["R1b1", "R1b2"],
        "R1b1": [],
        "R1b2": [],
        "C": [],
        "C~": [],
    }
    folder = root / "hg_prediction_tables"
    folder.mkdir(parents=True)
    # The yfull modules' layout (one root here): tree.json and
    # <build>/new_positions*.txt.
    (root / "tree.json").write_text(json.dumps(tree))
    for name in ("tree", "yfull_v10_tree", "ftdna_tree", "isogg_tree"):
        (folder / f"{name}.json").write_text(json.dumps(tree))
    for name in (
        "major_tables",
        "yfull_v10_major_tables",
        "ftdna_major_tables",
        "isogg_major_tables",
    ):
        tables = folder / name
        tables.mkdir()
        (tables / "Intermediates.txt").write_text("R\nC\n")
        for hg in ("R", "R1", "R1b"):
            (tables / f"{hg}_int.txt").write_text("R\tD\nC\tA\n")
        (tables / "C_int.txt").write_text("R\tA\nC\tD\n")
    for build in ("hg19", "hg38", "t2t"):
        (root / build).mkdir()
        for name in (
            "new_positions",
            "new_positions_ancient",
            f"ftdna_positions_{build}",
            f"ftdna_positions_ancient_{build}",
            f"isogg_positions_{build}",
            f"yfull_v10_positions_{build}",
        ):
            (root / build / f"{name}.txt").write_text(MARKERS)
    return root


@pytest.fixture
def native_tables(tmp_path, monkeypatch):
    root = make_data(tmp_path / "tables")
    monkeypatch.setattr(trees, "ensure_data", lambda *a, **k: root)
    monkeypatch.setattr(trees, "ensure_module", lambda *a, **k: root)
    monkeypatch.setattr(
        analysis,
        "manifest",
        lambda: {
            "upstream": {"tag": "v4.1.6"},
            "release_tag": "yleaf-data-v4.1.6",
        },
    )
    return root
