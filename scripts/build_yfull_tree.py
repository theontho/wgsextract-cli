#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a versioned YFull tree pack for native Yleaf from YFull's YTree.

Usage (from the repository root):

    pixi run -e dev python scripts/build_yfull_tree.py \
        --ytree scratch/YTree --version 14.06.0 \
        --ybrowse scratch/ybrowse/snps_hg38.csv \
        --chain-hg19 hg38ToHg19.over.chain.gz --chain-t2t hg38ToHs1.over.chain.gz \
        --ref-hg38 hs38DH.fa.gz --ref-hg19 hg19.fa.gz --ref-t2t chm13v2.0.fa.gz \
        --major-tables <yleaf data>/hg_prediction_tables/major_tables \
        --output out/yfull-tree/14.06.0 [--write-manifest]

--ytree is a clone of https://github.com/YFullTeam/YTree (CC BY 4.0); the
tree is read from its committed ``ytree/tree_<version>.zip``. Marker positions
come from a YBrowse ``snps_hg38.csv`` snapshot (http://ybrowse.org/gbrowse2/gff/),
which changes daily, so its SHA-256 and retrieval date are recorded.

For every YTree branch (``*`` paragroups are not branches) each SNP of the
branch, given as aliases ``A/B/C``, becomes one marker row when its aliases name
exactly one GRCh38 chrY single-base substitution in YBrowse. Rows are
- dropped when the aliases disagree on position or alleles;
- lifted to GRCh37 and T2T-CHM13 with the UCSC chains, complementing the
  alleles on a reverse-strand chain block, and dropped unless they lift to
  exactly one chrY position;
- dropped in each build unless that build's reference base is the ancestral or
  the derived allele;
- also written to ``new_positions_ancient.txt`` unless the mutation is C->T or
  G->A (post-mortem deamination; Yleaf v4.1.6's ancient-DNA rule).

The pack is a reproducible tar.gz; it, build_info.json, LICENSE-YTree.md and
their SHA256SUMS are the assets of the GitHub release
``yleaf-tree-yfull-v<version>``. --write-manifest pins the pack in trees.json.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyliftover import LiftOver

ROOT = Path(__file__).resolve().parents[1]
TREES_JSON = ROOT / "src/wgsextract_cli/ydna/yleaf_native/trees.json"
YTREE_REPO = "https://github.com/YFullTeam/YTree"
YBROWSE_URL = "http://ybrowse.org/gbrowse2/gff/snps_hg38.csv"
RELEASE_URL = "https://github.com/WGSExtract/wgsextract-cli/releases/download"
ROOT_KEY = 'ROOT (Y-Chromosome "Adam")'
BASES = frozenset("ACGT")
COMPLEMENT = str.maketrans("ACGT", "TGCA")
DEAMINATION = frozenset({"C->T", "G->A"})

Marker = tuple[str, str, int, str, str]  # name, haplogroup, pos, anc, der


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(checkout: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(checkout), *args], text=True
    ).strip()


def read_ytree(checkout: Path, version: str) -> tuple[dict[str, Any], dict[str, str]]:
    """The YTree JSON of ``version`` and where it came from."""
    member = f"ytree/tree_{version}.zip"
    zipped = checkout / member
    if not zipped.is_file():
        raise SystemExit(f"{zipped} not found; is --ytree a YTree clone?")
    commit = git(checkout, "log", "-1", "--format=%H", "--", member)
    date = git(checkout, "log", "-1", "--format=%cs", "--", member)
    with zipfile.ZipFile(zipped) as archive:
        names = [n for n in archive.namelist() if n.endswith(".json")]
        if len(names) != 1:
            raise SystemExit(f"{zipped} should hold one JSON tree, has {names}")
        tree: dict[str, Any] = json.loads(archive.read(names[0]))
    source = {
        "repository": YTREE_REPO,
        "commit": commit,
        "file": member,
        "sha256": sha256_file(zipped),
        "committed": date,
    }
    return tree, source


def flatten(tree: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Yleaf's flat ``{node: [children]}`` tree and each branch's SNP groups.

    YTree's root has an empty id; ``*`` nodes are paragroups (samples without
    a known sub-branch), not branches, so Yleaf's tree omits them.
    """
    children: dict[str, list[str]] = {}
    snps: dict[str, list[str]] = {}
    stack = [(ROOT_KEY, tree)]
    while stack:
        name, node = stack.pop()
        kids = [c for c in node.get("children", []) if not c["id"].endswith("*")]
        if name in children:
            raise SystemExit(f"Duplicate YTree branch {name}")
        children[name] = [kid["id"] for kid in kids]
        if name != ROOT_KEY:
            snps[name] = [
                group.strip()
                for group in str(node.get("snps", "")).split(",")
                if group.strip()
            ]
        stack.extend((kid["id"], kid) for kid in reversed(kids))
    return children, snps


def read_ybrowse(path: Path) -> dict[str, set[tuple[int, str, str]]]:
    """Each YBrowse name's GRCh38 chrY single-base substitutions."""
    sites: dict[str, set[tuple[int, str, str]]] = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            anc, der = row["allele_anc"].upper(), row["allele_der"].upper()
            if (
                row["seqid"] != "chrY"
                or row["start"] != row["end"]
                or anc not in BASES
                or der not in BASES
                or anc == der
            ):
                continue
            sites.setdefault(row["Name"], set()).add((int(row["start"]), anc, der))
    return sites


def resolve(
    snps: dict[str, list[str]], sites: dict[str, set[tuple[int, str, str]]]
) -> tuple[list[Marker], Counter[str]]:
    """One GRCh38 marker per SNP group whose aliases agree on one site."""
    markers: list[Marker] = []
    stats: Counter[str] = Counter()
    for haplogroup in sorted(snps):
        for group in snps[haplogroup]:
            stats["snp_groups"] += 1
            aliases = [a for a in group.split("/") if a]
            found = {site for a in aliases for site in sites.get(a, ())}
            if not found:
                stats["no_ybrowse_site"] += 1
                continue
            if len(found) > 1:
                stats["conflicting_sites"] += 1
                continue
            pos, anc, der = next(iter(found))
            name = next(a for a in aliases if a in sites)
            markers.append((name, haplogroup, pos, anc, der))
    return markers, stats


def lift(markers: list[Marker], chain: Path) -> tuple[list[Marker], Counter[str]]:
    lifter = LiftOver(str(chain))
    lifted: list[Marker] = []
    stats: Counter[str] = Counter()
    for name, haplogroup, pos, anc, der in markers:
        hits = lifter.convert_coordinate("chrY", pos - 1) or []
        hits = [hit for hit in hits if hit[0] == "chrY"]
        if len(hits) != 1:
            stats["unlifted"] += 1
            continue
        _, target, strand, _ = hits[0]
        if strand == "-":
            anc, der = anc.translate(COMPLEMENT), der.translate(COMPLEMENT)
        lifted.append((name, haplogroup, int(target) + 1, anc, der))
    return lifted, stats


def chr_y(reference: Path) -> str:
    """The reference's chrY sequence, upper-case (needs a samtools faidx index)."""
    fai = Path(f"{reference}.fai")
    contigs = {line.split("\t")[0] for line in fai.read_text().splitlines()}
    contig = next((c for c in ("chrY", "Y") if c in contigs), None)
    if contig is None:
        raise SystemExit(f"{reference} has no chrY")
    text = subprocess.check_output(
        ["samtools", "faidx", str(reference), contig], text=True
    )
    return "".join(text.splitlines()[1:]).upper()


def reference_checked(
    markers: list[Marker], sequence: str
) -> tuple[list[Marker], Counter[str]]:
    kept: list[Marker] = []
    stats: Counter[str] = Counter()
    for marker in markers:
        pos, anc, der = marker[2:]
        base = sequence[pos - 1] if 0 < pos <= len(sequence) else "N"
        if base not in (anc, der):
            stats["reference_mismatch"] += 1
            continue
        kept.append(marker)
    return kept, stats


def position_rows(markers: list[Marker], *, ancient: bool) -> Iterator[str]:
    for name, haplogroup, pos, anc, der in sorted(markers, key=lambda m: (m[2], m[1])):
        mutation = f"{anc}->{der}"
        if ancient and mutation in DEAMINATION:
            continue
        yield f"chrY\t{name}\t{haplogroup}\t{pos}\t{mutation}\t{anc}\t{der}\n"


def check_backbone(children: dict[str, list[str]], major_tables: Path) -> list[str]:
    """Yleaf's QC1 backbone groups the new tree lacks (should be none)."""
    nodes = set(children) | {kid for kids in children.values() for kid in kids}
    wanted = set()
    for table in major_tables.glob("*.txt"):
        for line in table.read_text(encoding="utf-8").splitlines():
            name = line.split("\t")[0].strip()
            if name and "~" not in name:
                wanted.add(name)
    return sorted(wanted - nodes)


def write_pack(files: dict[str, bytes], archive: Path) -> dict[str, Any]:
    """A byte-reproducible tar.gz and its manifest entry."""
    with archive.open("wb") as stream:
        with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for name in sorted(files):
                    info = tarfile.TarInfo(name)
                    info.size = len(files[name])
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(files[name]))
    return {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in sorted(files.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ytree", type=Path, required=True, help="YTree clone")
    parser.add_argument("--version", required=True, help="YTree version, e.g. 14.06.0")
    parser.add_argument("--ybrowse", type=Path, required=True, help="snps_hg38.csv")
    parser.add_argument(
        "--ybrowse-date",
        help="Date the YBrowse snapshot was downloaded (default: its mtime).",
    )
    parser.add_argument("--chain-hg19", type=Path, required=True)
    parser.add_argument("--chain-t2t", type=Path, required=True)
    parser.add_argument("--ref-hg38", type=Path, required=True)
    parser.add_argument("--ref-hg19", type=Path, required=True)
    parser.add_argument("--ref-t2t", type=Path, required=True)
    parser.add_argument(
        "--major-tables",
        type=Path,
        required=True,
        help="Yleaf's hg_prediction_tables/major_tables (QC1 backbone)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Pin the pack as the yfull tree in trees.json",
    )
    args = parser.parse_args()

    tree, source = read_ytree(args.ytree, args.version)
    children, snps = flatten(tree)
    missing = check_backbone(children, args.major_tables)
    if missing:
        raise SystemExit(f"YTree {args.version} lacks Yleaf backbone groups {missing}")
    print(f"YTree {args.version}: {len(children) - 1} branches")

    sites = read_ybrowse(args.ybrowse)
    hg38, stats = resolve(snps, sites)
    by_build: dict[str, list[Marker]] = {"hg38": hg38}
    build_stats: dict[str, dict[str, int]] = {}
    for build, chain in (("hg19", args.chain_hg19), ("t2t", args.chain_t2t)):
        by_build[build], build_stats[build] = lift(hg38, chain)
    build_stats["hg38"] = Counter()
    references = {"hg38": args.ref_hg38, "hg19": args.ref_hg19, "t2t": args.ref_t2t}
    for build, reference in references.items():
        by_build[build], checked = reference_checked(by_build[build], chr_y(reference))
        build_stats[build] = dict(Counter(build_stats[build]) + checked)
        build_stats[build]["markers"] = len(by_build[build])
        print(f"{build}: {build_stats[build]}")

    files: dict[str, bytes] = {
        "tree.json": json.dumps(children, separators=(",", ":")).encode(),
        "LICENSE-YTree.md": (args.ytree / "LICENSE.md").read_bytes(),
    }
    for build, markers in by_build.items():
        for ancient, name in (
            (False, "new_positions.txt"),
            (True, "new_positions_ancient.txt"),
        ):
            text = "".join(position_rows(markers, ancient=ancient))
            files[f"{build}/{name}"] = text.encode()
    ybrowse_date = (
        args.ybrowse_date
        or datetime.fromtimestamp(args.ybrowse.stat().st_mtime, timezone.utc)
        .date()
        .isoformat()
    )
    build_info = {
        "tree": {"version": args.version, **source, "branches": len(children) - 1},
        "positions": {
            "source": "YBrowse",
            "url": YBROWSE_URL,
            "retrieved": ybrowse_date,
            "sha256": sha256_file(args.ybrowse),
            "snp_groups": dict(stats),
        },
        "liftover": {
            build: {"chain": chain.name, "sha256": sha256_file(chain)}
            for build, chain in (("hg19", args.chain_hg19), ("t2t", args.chain_t2t))
        },
        "builds": build_stats,
        "script": "scripts/build_yfull_tree.py",
    }
    files["build_info.json"] = (json.dumps(build_info, indent=2) + "\n").encode()

    args.output.mkdir(parents=True, exist_ok=True)
    tag = f"yleaf-tree-yfull-v{args.version}"
    name = f"yfull-{args.version}.tar.gz"
    archive = args.output / name
    listing = write_pack(files, archive)
    released = [name, "build_info.json", "LICENSE-YTree.md"]
    for extra in released[1:]:
        (args.output / extra).write_bytes(files[extra])
    (args.output / "SHA256SUMS").write_text(
        "".join(f"{sha256_file(args.output / n)}  {n}\n" for n in released),
        encoding="utf-8",
        newline="\n",
    )
    entry = {
        "name": "YFull YTree",
        "version": args.version,
        "date": source["committed"],
        "license": "CC-BY-4.0",
        "source": f"{YTREE_REPO}/blob/{source['commit']}/{source['file']}",
        "positions": f"YBrowse snps_hg38.csv retrieved {ybrowse_date}",
        "url": f"{RELEASE_URL}/{tag}/{name}",
        "sha256": sha256_file(archive),
        "size": archive.stat().st_size,
        "files": listing,
    }
    (args.output / "trees_entry.json").write_text(
        json.dumps(entry, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"Wrote {archive} ({entry['size']} bytes, sha256 {entry['sha256']})")
    if args.write_manifest:
        manifest = json.loads(TREES_JSON.read_text(encoding="utf-8"))
        manifest["trees"]["yfull"] = {**manifest["trees"]["yfull"], **entry}
        TREES_JSON.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    main()
