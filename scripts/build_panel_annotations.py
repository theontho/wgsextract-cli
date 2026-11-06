"""Build the bundled gene annotations for `panel bed` (assets/panel/).

Usage (from the repository root):

    pixi run -e dev python scripts/build_panel_annotations.py --cache tmp/panel-sources

Downloads NCBI RefSeq's annotation of each build (pinned by SHA-256 in
SOURCES) into --cache, and writes src/wgsextract_cli/assets/panel/genes_<build>.tsv.gz:
one row per gene or pseudogene on the assembled chromosomes, with its 0-based
half-open span, NCBI Gene ID, biotype and synonyms. A gene in both
pseudoautosomal regions has a row on X and one on Y. NCBI data is public domain.

NCBI names mitochondrial genes without HGNC's "MT-" prefix (RNR1, COX1); the
rows use the HGNC symbol, from the MT-style synonym (MTRNR1 -> MT-RNR1).
NCBI's T2T-CHM13 annotation has no chrM; CHM13's chrM is as long as the rCRS
(16,569 bp) and colinear with it, so its rows take the rCRS spans.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "wgsextract_cli" / "assets" / "panel"
_NCBI = "https://ftp.ncbi.nlm.nih.gov/genomes/all/annotation_releases/9606"

SOURCES: dict[str, dict[str, str]] = {
    "hg38": {
        "release": "GCF_000001405.40-RS_2025_08",
        "prefix": "GCF_000001405.40_GRCh38.p14",
        "gff": "4920f0eae7e2197c50b67a201e06d657387137b49dd60f474b4f1d5b29334051",
        "report": "04e40091e427dae4f0c7dcc1b67792b8d4d5413903ea6b1d171840664e2da989",
    },
    "t2t": {
        "release": "GCF_009914755.1-RS_2025_08",
        "prefix": "GCF_009914755.1_T2T-CHM13v2.0",
        "gff": "833170d5445e5514537cc15ddec50381dc9798eae8eac3de97762cc6eac1a1cf",
        "report": "f4ce48c1e49139f21b7194501ecc19969d51d37291b7d4a8379da57afe2d9863",
    },
    "hg19": {
        "release": "GCF_000001405.25-RS_2024_09",
        "prefix": "GCF_000001405.25_GRCh37.p13",
        "gff": "5fcadac26be5d82a1f1c52e33cc5047247f719a60b50182c6ba298bda77cc80f",
        "report": "0097e6b24cc9ae5cb4fb5c075dffbae36b4ec0c90a61d428f039eb82e64e4a17",
    },
}
CHROMS = [f"chr{n}" for n in [*range(1, 23), "X", "Y", "M"]]
_MT_SYNONYM = re.compile(r"^MT([A-Z0-9]+)$")
# chrom, start, end, symbol, gene_id, biotype, synonyms
Row = tuple[str, int, int, str, str, str, str]


def fetch(url: str, sha256: str, cache: Path) -> Path:
    path = cache / url.rsplit("/", 1)[1]
    if not path.exists():
        print(f"downloading {url}")
        partial = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, partial)
        partial.rename(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        raise SystemExit(f"{path}: sha256 {digest}, expected {sha256}")
    return path


def contigs(report: Path) -> tuple[dict[str, str], dict[str, int]]:
    """RefSeq accession -> UCSC name, and UCSC name -> length, for CHROMS."""
    names, lengths = {}, {}
    for line in report.read_text().splitlines():
        if line.startswith("#"):
            continue
        f = line.split("\t")
        if f[1] != "assembled-molecule" or f[9] not in CHROMS:
            continue
        names[f[6]] = f[9]
        lengths[f[9]] = int(f[8])
    return names, lengths


def genes(gff: Path, names: dict[str, str]) -> list[Row]:
    rows: list[Row] = []
    with gzip.open(gff, "rt") as handle:
        for line in handle:
            f = line.rstrip("\n").split("\t")
            if len(f) < 9 or f[2] not in ("gene", "pseudogene") or f[0] not in names:
                continue
            attrs = {
                k: urllib.parse.unquote(v)
                for k, _, v in (a.partition("=") for a in f[8].split(";"))
            }
            symbol = attrs.get("gene") or attrs["Name"]
            synonyms = [s for s in attrs.get("gene_synonym", "").split(",") if s]
            chrom = names[f[0]]
            if chrom == "chrM":
                hgnc = next(
                    (f"MT-{m[1]}" for s in synonyms if (m := _MT_SYNONYM.match(s))),
                    None,
                )
                if hgnc:
                    synonyms = [symbol, *(s for s in synonyms if s != hgnc)]
                    symbol = hgnc
            gene_id = next(
                (
                    x.split(":", 1)[1]
                    for x in attrs.get("Dbxref", "").split(",")
                    if x.startswith("GeneID:")
                ),
                "",
            )
            rows.append(
                (
                    chrom,
                    int(f[3]) - 1,
                    int(f[4]),
                    symbol,
                    gene_id,
                    attrs.get("gene_biotype", f[2]),
                    ",".join(synonyms),
                )
            )
    return rows


def write(
    build: str, rows: list[Row], lengths: dict[str, int], notes: list[str]
) -> None:
    order = {c: i for i, c in enumerate(CHROMS)}
    rows = sorted(set(rows), key=lambda r: (order[r[0]], r[1], r[2], r[3]))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"genes_{build}.tsv.gz"
    header = [f"# {note}" for note in notes]
    header += [f"#contig\t{c}\t{lengths[c]}" for c in CHROMS if c in lengths]
    header.append("chrom\tstart\tend\tsymbol\tgene_id\tbiotype\tsynonyms")
    body = "\n".join([*header, *("\t".join(map(str, r)) for r in rows)]) + "\n"
    # mtime=0: the same input gives the same bytes.
    with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as out:
        out.write(body.encode())
    print(f"{path}: {len(rows)} genes, {path.stat().st_size} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cache", type=Path, default=ROOT / "tmp" / "panel-sources")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    built = {}
    for build, src in SOURCES.items():
        base = f"{_NCBI}/{src['release']}/{src['prefix']}"
        gff = fetch(f"{base}_genomic.gff.gz", src["gff"], args.cache)
        report = fetch(f"{base}_assembly_report.txt", src["report"], args.cache)
        names, lengths = contigs(report)
        built[build] = (genes(gff, names), lengths, src, base)
    mito = [r for r in built["hg38"][0] if r[0] == "chrM"]
    for build, (rows, lengths, src, base) in built.items():
        notes = [
            f"Genes and pseudogenes of NCBI RefSeq annotation {src['release']} "
            f"({src['prefix']}); public domain.",
            f"Source: {base}_genomic.gff.gz (sha256 {src['gff']}).",
            "Spans are 0-based, half-open. Built by scripts/build_panel_annotations.py.",
        ]
        if build == "t2t" and not any(r[0] == "chrM" for r in rows):
            rows = rows + mito
            lengths = {**lengths, "chrM": built["hg38"][1]["chrM"]}
            notes.append("chrM spans are the rCRS's (GRCh38 annotation).")
        write(build, rows, lengths, notes)


if __name__ == "__main__":
    main()
