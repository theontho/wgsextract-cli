#!/usr/bin/env python3
"""GIAB HG002 ONT R10.4.1 validation data (#299).

``fetch`` downloads the release-hosted slices listed in
``docs/data/ont_hg002_manifest.json`` and verifies their SHA-256.

``fixture`` rebuilds the committed CI fixture in ``tests/fixtures/ont_hg002``:
reads from ONT's public HG002 alignment, trimmed to a 12 kb window of chr20
around a homozygous 313 bp deletion, with their 5mC/5hmC MM/ML tags rewritten
for the trimmed sequence, every second read dropped (about 18x), the
reference window, and the GIAB truth in it.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "data" / "ont_hg002_manifest.json"
FIXTURE = ROOT / "tests" / "fixtures" / "ont_hg002"

CRAM_URL = (
    "https://ont-open-data.s3.amazonaws.com/giab_2023.05/analysis/hg002/sup/"
    "PAO83395.pass.cram"
)
GIAB = "https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/AshkenazimTrio/HG002_NA24385_son"
SMALL_TRUTH_URL = f"{GIAB}/NISTv4.2.1/GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz"
SV_TRUTH_URL = f"{GIAB}/v5.0q/HG002_GRCh38_v5.0q_stvar.vcf.gz"
CONTIG = "chr20"
WINDOW = (14_542_000, 14_554_000)  # 0-based, half-open
MIN_READ = 500
SV_INFO = ("SVTYPE", "SVLEN")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(outdir: Path, names: list[str]) -> None:
    from wgsextract_cli.core.ref_library.downloads import download_file

    manifest = json.loads(MANIFEST.read_text())
    outdir.mkdir(parents=True, exist_ok=True)
    for asset in manifest["assets"]:
        if names and asset["name"] not in names:
            continue
        dest = outdir / asset["name"]
        if dest.exists() and _sha256(dest) == asset["sha256"]:
            print(f"{dest}: verified")
            continue
        if not download_file(asset["url"], str(dest), expected_sha256=asset["sha256"]):
            sys.exit(f"{asset['url']}: download failed or SHA-256 mismatch")
        print(f"{dest}: downloaded and verified")


def _modification_tags(read, start: int, end: int) -> list[str]:
    """MM/ML for bases [start, end) of the read as sequenced."""
    seq = read.get_forward_sequence()[start:end]
    mm, ml = [], []
    for (base, strand, code), calls in sorted(read.modified_bases_forward.items()):
        if strand != 0:
            raise ValueError(f"{read.query_name}: unexpected {base}-{code} strand")
        kept = sorted((p - start, q) for p, q in calls if start <= p < end)
        assert all(seq[p] == base for p, _ in kept), read.query_name
        canonical = [i for i, b in enumerate(seq) if b == base]
        rank = {p: i for i, p in enumerate(canonical)}
        skips, last = [], -1
        for p, q in kept:
            skips.append(str(rank[p] - last - 1))
            last = rank[p]
            ml.append(str(q))
        mm.append(",".join([f"{base}+{code}?", *skips]))
    if not mm:
        return []
    return ["MM:Z:" + ";".join(mm) + ";", "ML:B:C," + ",".join(ml)]


def _trimmed_reads(cram: str, ref: str) -> list[str]:
    import pysam

    lo, hi = WINDOW
    records = []
    with pysam.AlignmentFile(cram, reference_filename=ref) as alignments:
        for read in alignments.fetch(CONTIG, lo, hi):
            if read.is_secondary or read.is_supplementary or read.is_unmapped:
                continue
            if int(hashlib.sha256(read.query_name.encode()).hexdigest(), 16) % 2:
                continue
            inside = [
                q for q, r in read.get_aligned_pairs(matches_only=True) if lo <= r < hi
            ]
            if not inside:
                continue
            first, last = min(inside), max(inside) + 1
            length = read.query_length
            if read.is_reverse:
                first, last = length - last, length - first
            if last - first < MIN_READ:
                continue
            seq = read.get_forward_sequence()[first:last]
            qual = read.get_forward_qualities()[first:last]
            tags = _modification_tags(read, first, last)
            header = "\t".join([f"@{read.query_name}", *tags])
            quals = "".join(chr(q + 33) for q in qual)
            records.append(f"{header}\n{seq}\n+\n{quals}\n")
    return sorted(records)


def _truth(url: str) -> list[str]:
    """Truth records inside the window, shifted to its coordinates, with
    only GT and the SVTYPE/SVLEN INFO fields."""
    lo, hi = WINDOW
    region = f"{CONTIG}:{lo + 1}-{hi}"
    out = subprocess.run(
        ["bcftools", "view", "-H", "-r", region, url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        pos = int(f[1])
        if pos <= lo or pos + len(f[3]) - 1 > hi or "*" in f[4]:
            continue
        if url == SV_TRUTH_URL and abs(len(f[3]) - len(f[4])) < 50:
            continue
        info = [kv for kv in f[7].split(";") if kv.split("=")[0] in SV_INFO]
        rows.append(
            [CONTIG, str(pos - lo), ".", f[3], f[4], ".", "PASS"]
            + [";".join(info) or ".", "GT", f[9].split(":")[0]]
        )
    return ["\t".join(r) for r in rows]


def fixture(cram: str, ref: str) -> None:
    import pysam

    lo, hi = WINDOW
    FIXTURE.mkdir(parents=True, exist_ok=True)
    window = pysam.FastaFile(ref).fetch(CONTIG, lo, hi).upper()
    fasta = FIXTURE / "ref.fa"
    lines = [window[i : i + 60] for i in range(0, len(window), 60)]
    fasta.write_text(f">{CONTIG}\n" + "\n".join(lines) + "\n")

    reads = FIXTURE / "reads.fastq.gz"
    with gzip.GzipFile(reads, "wb", mtime=0) as handle:
        handle.write("".join(_trimmed_reads(cram, ref)).encode())

    header = [
        "##fileformat=VCFv4.2",
        f"##contig=<ID={CONTIG},length={hi - lo}>",
        '##INFO=<ID=SVTYPE,Number=1,Type=String,Description="SV type">',
        '##INFO=<ID=SVLEN,Number=.,Type=Integer,Description="SV length">',
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tHG002",
    ]
    for name, url in (
        ("truth_small.vcf", SMALL_TRUTH_URL),
        ("truth_sv.vcf", SV_TRUTH_URL),
    ):
        (FIXTURE / name).write_text("\n".join(header + _truth(url)) + "\n")

    files = sorted(p for p in FIXTURE.iterdir() if p.name != "provenance.json")
    provenance = {
        "sample": "HG002 (GIAB NA24385)",
        "reads": {
            "url": CRAM_URL,
            "license": "CC BY 4.0 (Oxford Nanopore Technologies open data)",
            "basecaller": "dna_r10.4.1_e8.2_400bps_sup@v4.0.0",
            "selection": "primary alignments overlapping the window, trimmed to "
            "their aligned span inside it, reads whose SHA-256 of the name is "
            f"odd dropped, pieces under {MIN_READ} bp dropped; MM/ML rewritten "
            "for the trimmed sequence, in the sequencing orientation",
        },
        "reference": "GRCh38 no-alt analysis set (hs38), "
        f"{CONTIG}:{lo + 1}-{hi} as contig {CONTIG}",
        "coordinate_offset": lo,
        "truth": {"small": SMALL_TRUTH_URL, "sv": SV_TRUTH_URL},
        "builder": "scripts/ont_hg002_data.py fixture",
        "sha256": {p.name: _sha256(p) for p in files},
    }
    (FIXTURE / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    for p in files:
        print(f"{p}: {p.stat().st_size} bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("fetch", help="Download and verify the release slices.")
    get.add_argument("--outdir", type=Path, default=ROOT / "out" / "ont_hg002")
    get.add_argument("names", nargs="*", help="Assets to fetch (default: all).")
    build = sub.add_parser("fixture", help="Rebuild tests/fixtures/ont_hg002.")
    build.add_argument("--cram", default=CRAM_URL, help="ONT HG002 CRAM or URL.")
    build.add_argument("--ref", required=True, help="GRCh38 no-alt FASTA (hs38).")
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.outdir, args.names)
    else:
        fixture(args.cram, args.ref)


if __name__ == "__main__":
    main()
