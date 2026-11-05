"""scripts/build_yfull_tree.py turns a YTree release into a Yleaf tree pack (#359)."""

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIZE = 100
YTREE = {
    "id": "",
    "children": [
        {
            "id": "R",
            "snps": "M1",
            "children": [
                {
                    "id": "R1",
                    "snps": "M2/M2b, M3/M3x, M9",
                    "children": [{"id": "R1a", "snps": "M4"}, {"id": "R1*"}],
                }
            ],
        }
    ],
}
# GRCh38 sites: M3's aliases disagree, M9 is not in YBrowse.
YBROWSE = [
    ("M1", 10, "A", "G"),
    ("M2", 30, "C", "T"),
    ("M2b", 30, "C", "T"),
    ("M3", 40, "G", "A"),
    ("M3x", 41, "G", "A"),
    ("M4", 50, "G", "A"),
    ("M5", 60, "A", "AT"),
]


def _fasta(path: Path, bases: dict[int, str], length: int) -> Path:
    sequence = ["N"] * length
    for pos, base in bases.items():
        sequence[pos - 1] = base
    path.write_text(f">chrY\n{''.join(sequence)}\n")
    subprocess.run(["samtools", "faidx", str(path)], check=True)
    return path


def _inputs(tmp_path: Path) -> list[str]:
    ytree = tmp_path / "YTree"
    (ytree / "ytree").mkdir(parents=True)
    with zipfile.ZipFile(ytree / "ytree/tree_1.0.0.zip", "w") as archive:
        archive.writestr("tree_1.0.0.json", json.dumps(YTREE))
    (ytree / "LICENSE.md").write_text("CC BY 4.0\n")
    git = ["git", "-C", str(ytree), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "tree"], check=True)
    ybrowse = tmp_path / "snps_hg38.csv"
    ybrowse.write_text(
        "seqid,source,type,start,end,Name,allele_anc,allele_der\n"
        + "".join(
            f"chrY,point,snp,{pos},{pos},{name},{anc},{der}\n"
            for name, pos, anc, der in YBROWSE
        )
    )
    # hg19 is GRCh38 shifted by 10; T2T is GRCh38 reverse-complemented.
    hg19_chain = tmp_path / "hg38ToHg19.over.chain"
    hg19_chain.write_text(
        f"chain 1 chrY {SIZE} + 0 {SIZE} chrY {SIZE + 10} + 10 {SIZE + 10} 1\n{SIZE}\n\n"
    )
    t2t_chain = tmp_path / "hg38ToHs1.over.chain"
    t2t_chain.write_text(
        f"chain 1 chrY {SIZE} + 0 {SIZE} chrY {SIZE} - 0 {SIZE} 2\n{SIZE}\n\n"
    )
    hg38 = _fasta(tmp_path / "hg38.fa", {10: "A", 30: "C", 50: "G"}, SIZE)
    # M4's hg19 reference base is neither allele, so hg19 drops it.
    hg19 = _fasta(tmp_path / "hg19.fa", {20: "A", 40: "C", 60: "T"}, SIZE + 10)
    t2t = _fasta(tmp_path / "t2t.fa", {91: "T", 71: "G", 51: "C"}, SIZE)
    tables = tmp_path / "major_tables"
    tables.mkdir()
    (tables / "Intermediates.txt").write_text("R\nR~\n")
    return [
        "--ytree", str(ytree), "--version", "1.0.0", "--ybrowse", str(ybrowse),
        "--ybrowse-date", "2026-11-05", "--chain-hg19", str(hg19_chain),
        "--chain-t2t", str(t2t_chain), "--ref-hg38", str(hg38),
        "--ref-hg19", str(hg19), "--ref-t2t", str(t2t), "--major-tables", str(tables),
    ]  # fmt: skip


def _build(args: list[str], output: Path) -> dict[str, bytes]:
    subprocess.run(
        [
            sys.executable,
            ROOT / "scripts/build_yfull_tree.py",
            *args,
            "--output",
            output,
        ],
        check=True,
        capture_output=True,
    )
    with tarfile.open(output / "yfull-1.0.0.tar.gz") as pack:
        return {
            member.name: pack.extractfile(member).read()  # type: ignore[union-attr]
            for member in pack.getmembers()
        }


@pytest.mark.skipif(not shutil.which("samtools"), reason="samtools required")
def test_pack_holds_tree_and_build_marker_tables_reproducibly(tmp_path: Path) -> None:
    args = _inputs(tmp_path)
    files = _build(args, tmp_path / "a")

    assert json.loads(files["tree.json"]) == {
        'ROOT (Y-Chromosome "Adam")': ["R"],
        "R": ["R1"],
        "R1": ["R1a"],
        "R1a": [],
    }
    assert files["hg38/new_positions.txt"].decode() == (
        "chrY\tM1\tR\t10\tA->G\tA\tG\n"
        "chrY\tM2\tR1\t30\tC->T\tC\tT\n"
        "chrY\tM4\tR1a\t50\tG->A\tG\tA\n"
    )
    # Deamination-like C->T and G->A markers are not used for ancient DNA.
    assert files["hg38/new_positions_ancient.txt"].decode() == (
        "chrY\tM1\tR\t10\tA->G\tA\tG\n"
    )
    assert files["hg19/new_positions.txt"].decode() == (
        "chrY\tM1\tR\t20\tA->G\tA\tG\nchrY\tM2\tR1\t40\tC->T\tC\tT\n"
    )
    # Reverse-strand liftover complements the alleles.
    assert files["t2t/new_positions.txt"].decode() == (
        "chrY\tM4\tR1a\t51\tC->T\tC\tT\n"
        "chrY\tM2\tR1\t71\tG->A\tG\tA\n"
        "chrY\tM1\tR\t91\tT->C\tT\tC\n"
    )
    info = json.loads(files["build_info.json"])
    assert info["positions"]["snp_groups"] == {
        "snp_groups": 5,
        "conflicting_sites": 1,
        "no_ybrowse_site": 1,
    }
    assert info["builds"]["hg19"]["reference_mismatch"] == 1

    first = (tmp_path / "a/yfull-1.0.0.tar.gz").read_bytes()
    _build(args, tmp_path / "b")
    assert (tmp_path / "b/yfull-1.0.0.tar.gz").read_bytes() == first
    entry = json.loads((tmp_path / "a/trees_entry.json").read_text())
    assert entry["sha256"] == hashlib.sha256(first).hexdigest()
    assert entry["files"]["tree.json"]["sha256"] == (
        hashlib.sha256(files["tree.json"]).hexdigest()
    )
    sums = (tmp_path / "a/SHA256SUMS").read_text().splitlines()
    assert [line.split()[1] for line in sums] == [
        "yfull-1.0.0.tar.gz",
        "build_info.json",
        "LICENSE-YTree.md",
    ]


@pytest.mark.skipif(not shutil.which("samtools"), reason="samtools required")
def test_tree_missing_a_yleaf_backbone_group_is_rejected(tmp_path: Path) -> None:
    args = _inputs(tmp_path)
    (tmp_path / "major_tables/Intermediates.txt").write_text("R\nQ\n")
    result = subprocess.run(
        [sys.executable, ROOT / "scripts/build_yfull_tree.py", *args,
         "--output", tmp_path / "out"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert result.returncode != 0 and "['Q']" in result.stderr
    assert not (tmp_path / "out").exists()
