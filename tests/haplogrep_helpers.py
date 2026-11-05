"""A tiny Haplogrep 3 tree, served as the pinned trees' download, so real
Haplogrep 3 classifies against it offline."""

import hashlib
import random
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from wgsextract_cli.core import haplogrep_trees
from wgsextract_cli.core.config import settings

# Each haplogroup's defining positions; A1 and B1 descend from A and B.
TREE = {"A": (100, 200), "A1": (300,), "B": (400, 500), "B1": (600,)}
MT_LENGTH = 16_569


@dataclass
class MiniTree:
    mito: str
    downloads: list[str]

    def alt(self, pos: int) -> str:
        """The derived allele at ``pos``."""
        return "ACGT"[("ACGT".index(self.mito[pos - 1]) + 1) % 4]

    def vcf_body(self, positions, chrom: str = "chrM", sample: str = "1") -> str:
        return "".join(
            f"{chrom}\t{p}\t.\t{self.mito[p - 1]}\t{self.alt(p)}\t50\tPASS\t.\t"
            f"GT\t{sample}\n"
            for p in positions
        )


def _tree_files(tree_id: str, mito: str, alt) -> dict[str, str]:
    def group(name: str, children: str = "") -> str:
        polys = "".join(f"<poly>{p}{alt(p)}</poly>" for p in TREE[name])
        return (
            f'<haplogroup name="{name}"><details accessionNr="" reference="">'
            f"{polys}</details>{children}</haplogroup>\n"
        )

    return {
        "tree.yaml": f"id: {tree_id}\nversion: 1.0\nname: Mini tree\ntree: tree.xml\n"
        "weights: weights.txt\nfasta: rcrs.fasta\ngff: mtdna.gff\n"
        "aacTable: mtdna.aac.txt\n",
        "tree.xml": '<?xml version="1.0" encoding="UTF-8"?>\n<phylotree>\n'
        + group("A", group("A1"))
        + group("B", group("B1"))
        + "</phylotree>\n",
        "weights.txt": "".join(
            f"{p}{alt(p)}\t1.0\t1.0\t1.0\t1.0\t1.0\n"
            for ps in TREE.values()
            for p in ps
        ),
        "rcrs.fasta": f">chrM\n{mito}\n",
        "mtdna.gff": "",
        "mtdna.aac.txt": "",
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def mini_tree(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MiniTree]:
    """Every pinned tree serves this tree, version 1.0; downloads are counted.
    Trees install into a fresh reference library."""
    library = tmp_path_factory.mktemp("library")
    monkeypatch.setitem(settings, "reference_library", str(library))
    rng = random.Random(7)
    tree = MiniTree("".join(rng.choice("ACGT") for _ in range(MT_LENGTH)), [])
    folder = tmp_path_factory.mktemp("trees")
    trees = {}
    for tree_id in haplogrep_trees.TREE_IDS:
        files = {
            name: text.encode()
            for name, text in _tree_files(tree_id, tree.mito, tree.alt).items()
        }
        archive = folder / f"{tree_id}.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            for name, data in files.items():
                stream.writestr(name, data)
        trees[tree_id] = {
            "version": "1.0",
            "name": f"Mini {tree_id}",
            "url": f"https://example.invalid/{tree_id}.zip",
            "sha256": _sha256(archive.read_bytes()),
            "files": {
                name: {"sha256": _sha256(data), "size": len(data)}
                for name, data in files.items()
            },
        }
    monkeypatch.setitem(haplogrep_trees.MANIFEST, "trees", trees)

    def download(url: str, dest: str, **_kwargs) -> bool:
        tree.downloads.append(url)
        shutil.copyfile(folder / Path(url).name, dest)
        return True

    # The network is the boundary: serve the tree archives locally.
    monkeypatch.setattr(haplogrep_trees, "download_file", download)
    yield tree
