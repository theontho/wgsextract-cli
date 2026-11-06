#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the report's gene lists, with each gene's hg19 and hg38 span.

Usage (from the repository root):

    pixi run -e dev python scripts/build_report_gene_lists.py \
        --output out/report-gene-lists [--write-manifest]

The curated lists are ``scripts/report_gene_lists/<id>.tsv`` (gene,
category, note: facts from the cited paper's table, notes paraphrased). Spans
(1-based, inclusive) merge each gene's curated RefSeq (NM_/NR_)
transcripts on the primary chromosomes of the pinned annotation releases below, verified against NCBI's
md5checksums.txt. Each span is cross-checked against UCSC refGene (all RefSeq
transcripts of the symbol merged, as ``wgsextract ref gene-map`` reads them);
build_info.json records every difference. A gene missing from either release,
or found on two chromosomes, stops the build.

NCBI RefSeq annotation and UCSC refGene are public data without use
restrictions. The outputs, build_info.json and SHA256SUMS are the assets of the
GitHub release ``report-gene-lists-v<N>``; --write-manifest pins them in
src/wgsextract_cli/assets/report/gene_lists.json under --release, with each
list's symbols and categories (so ``panel bed`` presets work offline).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import urllib.request
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURATED = ROOT / "scripts" / "report_gene_lists"
MANIFEST = ROOT / "src" / "wgsextract_cli" / "assets" / "report" / "gene_lists.json"
REPO = "https://github.com/WGSExtract/wgsextract-cli"
NCBI = "https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/9606"
RELEASES = {
    "hg38": (
        "NCBI RefSeq annotation release GCF_000001405.40-RS_2025_08 (GRCh38.p14)",
        f"{NCBI}/GCF_000001405.40-RS_2025_08",
        "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
    ),
    "hg19": (
        "NCBI RefSeq annotation release 105.20220307 (GRCh37.p13)",
        f"{NCBI}/105.20220307/GCF_000001405.25_GRCh37.p13",
        "GCF_000001405.25_GRCh37.p13_genomic.gff.gz",
    ),
}
UCSC = "https://hgdownload.soe.ucsc.edu/goldenPath/{build}/database/refGene.txt.gz"
COLUMNS = ("gene", "category", "hg19_chrom", "hg19_start", "hg19_end",
           "hg38_chrom", "hg38_start", "hg38_end", "note")  # fmt: skip
# Each list's display metadata; the curated file's header carries the source.
LISTS = {
    "acmg-sf-3.3": {
        "name": "ACMG SF v3.3",
        "version": "3.3",
        "file": "acmg_sf_v3.3.tsv",
        "citation": "Lee K et al. ACMG SF v3.3 list for reporting of secondary "
        "findings in clinical exome and genome sequencing. Genet Med "
        "2025;27(8):101454. doi:10.1016/j.gim.2025.101454. PMID 40568962",
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12318660/",
    },
    "acmg-sf-3.2": {
        "name": "ACMG SF v3.2",
        "version": "3.2",
        "file": "acmg_sf_v3.2.tsv",
        "citation": "Miller DT et al. ACMG SF v3.2 list for reporting of secondary "
        "findings in clinical exome and genome sequencing. Genet Med "
        "2023;25(8):100866. doi:10.1016/j.gim.2023.100866. PMID 37347242",
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10524344/",
    },
}
Span = tuple[str, int, int]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"Downloading {url}")
        part = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url) as response, part.open("wb") as out:
            while block := response.read(1 << 20):
                out.write(block)
        part.replace(dest)
    return dest


def _ncbi_gff(build: str, cache: Path) -> Path:
    _, base, name = RELEASES[build]
    gff = _fetch(f"{base}/{name}", cache / name)
    sums = _fetch(f"{base}/md5checksums.txt", cache / f"{build}-md5checksums.txt")
    expected = next(
        line.split()[0]
        for line in sums.read_text().splitlines()
        if line.split()[-1].lstrip("./") == name
    )
    digest = hashlib.md5()
    with gff.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        gff.unlink()
        raise SystemExit(f"{name}: MD5 differs from NCBI's md5checksums.txt")
    return gff


def _chromosome(accession: str) -> str | None:
    """chr1..chr22, chrX, chrY for RefSeq NC_0000NN chromosome accessions."""
    match = re.fullmatch(r"NC_0000(\d\d)\.\d+", accession)
    if not match:
        return None
    number = int(match.group(1))
    return {23: "chrX", 24: "chrY"}.get(number, f"chr{number}")


def ncbi_spans(gff: Path, symbols: set[str]) -> dict[str, list[Span]]:
    """Each symbol's curated RefSeq (NM_/NR_) transcript features, the models
    UCSC refGene carries; the gene features also span predicted XM_ models."""
    spans: dict[str, list[Span]] = {}
    with gzip.open(gff, "rt") as stream:
        for line in stream:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 9 or cols[2] in ("gene", "exon", "CDS"):
                continue
            chrom = _chromosome(cols[0])
            gene = re.search(r"(?:^|;)gene=([^;]+)", cols[8])
            if (
                chrom
                and gene
                and gene.group(1) in symbols
                and re.search(r"(?:^|;)transcript_id=N[MR]_", cols[8])
            ):
                span = (chrom, int(cols[3]), int(cols[4]))
                spans.setdefault(gene.group(1), []).append(span)
    return spans


def ucsc_spans(build: str, cache: Path, symbols: set[str]) -> dict[str, Span]:
    path = _fetch(UCSC.format(build=build), cache / f"refGene_{build}.txt.gz")
    merged: dict[str, Span] = {}
    with gzip.open(path, "rt") as stream:
        for line in stream:
            cols = line.split("\t")
            chrom, symbol = cols[2], cols[12]
            if symbol not in symbols or "_" in chrom:
                continue
            start, end = int(cols[4]) + 1, int(cols[5])
            old = merged.get(symbol)
            if old and old[0] != chrom:
                continue
            merged[symbol] = (
                (chrom, min(old[1], start), max(old[2], end))
                if old
                else (chrom, start, end)
            )
    return merged


def curated(list_id: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = (CURATED / LISTS[list_id]["file"]).read_text().splitlines()
    header = [line for line in lines if line.startswith("#")]
    body = [line.split("\t") for line in lines if not line.startswith("#")]
    names = body[0]
    # An empty trailing note leaves the row one cell short.
    padded = (row + [""] * (len(names) - len(row)) for row in body[1:])
    return header, [dict(zip(names, row, strict=True)) for row in padded]


def _rows(
    genes: list[dict[str, str]], spans: dict[str, dict[str, Span]]
) -> Iterator[str]:
    for gene in genes:
        cells = [gene["gene"], gene["category"]]
        for build in ("hg19", "hg38"):
            chrom, start, end = spans[build][gene["gene"]]
            cells += [chrom, str(start), str(end)]
        yield "\t".join([*cells, gene["note"]])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, help="downloads (default: OUTPUT/cache)")
    parser.add_argument("--release", default="report-gene-lists-v1")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()
    cache = args.cache or args.output / "cache"
    cache.mkdir(parents=True, exist_ok=True)

    lists = {list_id: curated(list_id) for list_id in LISTS}
    symbols = {g["gene"] for _, genes in lists.values() for g in genes}
    spans: dict[str, dict[str, Span]] = {}
    differences: dict[str, dict[str, dict[str, Span | None]]] = {}
    for build in RELEASES:
        found = ncbi_spans(_ncbi_gff(build, cache), symbols)
        problems = sorted(symbols - set(found)) + sorted(
            s for s, v in found.items() if len({c for c, _, _ in v}) != 1
        )
        if problems:
            raise SystemExit(
                f"{build}: genes missing or on two chromosomes: {problems}"
            )
        spans[build] = {
            s: (v[0][0], min(a for _, a, _ in v), max(b for _, _, b in v))
            for s, v in found.items()
        }
        ucsc = ucsc_spans(build, cache, symbols)
        differences[build] = {
            s: {"ncbi": spans[build][s], "ucsc_refgene": ucsc.get(s)}
            for s in sorted(symbols)
            if ucsc.get(s) != spans[build][s]
        }
        print(f"{build}: {len(differences[build])} spans differ from UCSC refGene")

    args.output.mkdir(parents=True, exist_ok=True)
    sources = [
        f"# Gene spans (1-based, inclusive): curated RefSeq (NM_/NR_) transcripts "
        f"merged, from "
        f"{RELEASES['hg19'][0]}",
        f"# and {RELEASES['hg38'][0]}.",
        "# `wgsextract report` lists ClinVar pathogenic/likely-pathogenic calls in "
        "these genes.",
        "# The list is a technical filter, not a clinical test.",
    ]
    outputs: dict[str, Path] = {}
    for list_id, (header, genes) in lists.items():
        path = args.output / LISTS[list_id]["file"]
        text = [*header, *sources, "\t".join(COLUMNS), *_rows(genes, spans)]
        path.write_text("\n".join(text) + "\n", encoding="utf-8", newline="\n")
        outputs[list_id] = path
    info = {
        "spans": {
            b: {"source": r[0], "url": f"{r[1]}/{r[2]}"} for b, r in RELEASES.items()
        },
        "cross_check": {
            "against": "UCSC refGene (hg19, hg38), RefSeq transcripts merged",
            "differences": differences,
        },
        "lists": {
            i: {"file": p.name, "sha256": _sha256(p)} for i, p in outputs.items()
        },
    }
    info_path = args.output / "build_info.json"
    info_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    assets = [*outputs.values(), info_path]
    (args.output / "SHA256SUMS").write_text(
        "".join(f"{_sha256(p)}  {p.name}\n" for p in assets), encoding="utf-8"
    )
    print(f"Wrote {', '.join(p.name for p in assets)} and SHA256SUMS to {args.output}")

    if args.write_manifest:
        manifest = {
            "schema_version": 1,
            "default": next(iter(LISTS)),
            "lists": {
                list_id: {
                    **{k: v for k, v in meta.items() if k != "file"},
                    # Symbols and categories, for use offline (panel presets)
                    "genes": {g["gene"]: g["category"] for g in lists[list_id][1]},
                    "spans": f"{RELEASES['hg19'][0]}; {RELEASES['hg38'][0]}",
                    "download": f"{REPO}/releases/download/{args.release}/{meta['file']}",
                    "sha256": _sha256(outputs[list_id]),
                    "size": outputs[list_id].stat().st_size,
                }
                for list_id, meta in LISTS.items()
            },
        }
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Pinned {args.release} in {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
