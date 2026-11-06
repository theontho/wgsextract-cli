#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the report's gene lists, with each gene's hg19 and hg38 span.

Usage (from the repository root):

    pixi run -e dev python scripts/build_report_gene_lists.py \
        --output out/report-gene-lists [--write-manifest]

The curated lists are ``scripts/report_gene_lists/<id>.tsv`` (gene,
category, note: facts from the cited paper's table, notes paraphrased). Spans
(1-based, inclusive) are each gene's MANE Select (GRCh38) or RefSeq Select
(GRCh37) transcript on the primary chromosomes of the pinned annotation
releases below, verified against NCBI's md5checksums.txt. Each span is
cross-checked against UCSC refGene (all RefSeq transcripts of the symbol
merged, as ``wgsextract ref gene-map`` reads them); build_info.json records
every difference. A gene missing from either release, found on two
chromosomes, or more than MAX_REFGENE_DIFFERENCE from refGene at either end
stops the build.

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


# One representative transcript per gene, the first tag a gene has: GRCh38
# releases tag MANE Select, older GRCh37 ones RefSeq Select. Merging every
# transcript instead lets readthrough models swallow neighbours (MSH2 into
# KCNK12, BRCA1 into NBR2).
SELECT_TAGS = ("MANE Select", "RefSeq Select")
# The most a span may differ from UCSC refGene (all transcripts merged) at
# either end before the build stops for review.
MAX_REFGENE_DIFFERENCE = 25_000
# Transcripts a gene's span also covers. APC promoter 1B (NM_001127511, ~30 kb
# upstream of the MANE start) carries the pathogenic GAPPS variants.
EXTRA_TRANSCRIPTS = {"APC": ("NM_001127511",)}
# Genes whose select span may differ from refGene by more than the limit,
# because refGene merges alternative transcripts beyond it (checked by hand
# against refGene's transcripts in both builds).
REFGENE_EXCEPTIONS = {
    "ACTA2": "refGene's NM_001141945 and NM_001320855 add an alternative first "
    "exon 38.6 kb upstream, inside FAS",
    "BTD": "refGene's NM_001370752 and NM_001370753 add 3' exons up to 68.8 kb "
    "downstream, inside ANKRD28",
    "LMNA": "refGene's NM_001282625 adds an alternative first exon 32 kb "
    "upstream of the MANE start",
    "MAX": "refGene's NM_001271069 and NM_197957 add 3' exons up to 69 kb "
    "downstream, inside FNTB",
}


def ncbi_spans(gff: Path, symbols: set[str]) -> dict[str, list[Span]]:
    """Each symbol's select transcript(s) on the primary chromosomes (see
    SELECT_TAGS), and its EXTRA_TRANSCRIPTS; a symbol with no select
    transcript is left out."""
    tagged: dict[str, dict[str, list[Span]]] = {}
    with gzip.open(gff, "rt") as stream:
        for line in stream:
            if line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 9 or cols[2] in ("gene", "exon", "CDS"):
                continue
            chrom = _chromosome(cols[0])
            gene = re.search(r"(?:^|;)gene=([^;]+)", cols[8])
            tag = re.search(r"(?:^|;)tag=([^;]+)", cols[8])
            transcript = re.search(r"(?:^|;)transcript_id=([^.;]+)", cols[8])
            tags = tag.group(1).split(",") if tag else []
            if (
                transcript
                and gene
                and transcript.group(1) in EXTRA_TRANSCRIPTS.get(gene.group(1), ())
            ):
                tags.append("extra")
            if chrom and gene and tags and gene.group(1) in symbols:
                span = (chrom, int(cols[3]), int(cols[4]))
                for name in tags:
                    tagged.setdefault(gene.group(1), {}).setdefault(name, []).append(
                        span
                    )
    return {
        symbol: next(by_tag[t] for t in SELECT_TAGS if t in by_tag)
        + by_tag.get("extra", [])
        for symbol, by_tag in tagged.items()
        if any(t in by_tag for t in SELECT_TAGS)
    }


def _too_far(ours: Span | None, theirs: Span | None) -> bool:
    return (
        ours is None
        or theirs is None
        or ours[0] != theirs[0]
        or abs(ours[1] - theirs[1]) > MAX_REFGENE_DIFFERENCE
        or abs(ours[2] - theirs[2]) > MAX_REFGENE_DIFFERENCE
    )


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
    parser.add_argument("--release", default="report-gene-lists-v2")
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
        too_far = [
            s
            for s, d in differences[build].items()
            if s not in REFGENE_EXCEPTIONS and _too_far(d["ncbi"], d["ucsc_refgene"])
        ]
        if too_far:
            raise SystemExit(
                f"{build}: spans more than {MAX_REFGENE_DIFFERENCE} bp from UCSC "
                f"refGene (or missing there): {too_far}"
            )

    # A gene's span lengths agree across builds unless an annotation changed.
    uneven = [
        s
        for s in sorted(symbols)
        if abs(
            (spans["hg38"][s][2] - spans["hg38"][s][1])
            - (spans["hg19"][s][2] - spans["hg19"][s][1])
        )
        > MAX_REFGENE_DIFFERENCE
    ]
    if uneven:
        raise SystemExit(f"hg19 and hg38 span lengths differ too much: {uneven}")

    args.output.mkdir(parents=True, exist_ok=True)
    sources = [
        "# Gene spans (1-based, inclusive): each gene's MANE Select (GRCh38) or "
        "RefSeq Select (GRCh37) transcript, from "
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
            "max_difference_bp": MAX_REFGENE_DIFFERENCE,
            "exceptions": REFGENE_EXCEPTIONS,
            "extra_transcripts": EXTRA_TRANSCRIPTS,
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
