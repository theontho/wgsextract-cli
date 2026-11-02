"""Build the packaged table of vendor strand quirks from public openSNP exports.

  alleles  Stream the GRCh37 exports of each catalogued chip revision from the
           openSNP 2017-12-08 dump (CC0) and write, per revision, how often
           each allele was reported at each marker. Only these aggregate
           counts are kept: no genotypes, names, IDs or header text.
  build    Compare the counts with the GRCh37 reference and with the other
           revisions, and write ``assets/microarray/revisions/strand.tsv``.

Usage:
  python scripts/microarray_strand_quirks.py alleles WORKDIR --corpus CORPUS
  python scripts/microarray_strand_quirks.py build WORKDIR --reference hs37d5.fa.gz

CORPUS is the ``microarray_chip_catalogue.py corpus`` work directory, whose
fingerprints assign each dump member to a catalogued revision.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import gzip
import io
import json
import sys
import threading
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from microarray_chip_catalogue import (  # noqa: E402
    DUMP_URL,
    MAX_MEMBER_BYTES,
    _HttpRange,
    _member_text,
)

from wgsextract_cli.core.microarray_quirks import STRAND_TABLE  # noqa: E402
from wgsextract_cli.core.microarray_revisions import (  # noqa: E402
    _COLUMN_LABELS,
    CATALOGUE,
    Revision,
    load_catalogue,
)
from wgsextract_cli.core.microarray_utils import (  # noqa: E402
    _resolve_templates_for_format,
    chr_to_int,
    get_template_format,
    template_rows,
    vendor_chrom,
)
from wgsextract_cli.core.utils import WGSExtractError  # noqa: E402

BASES = "ACGT"
# Enough exports to see both alleles of any common SNP.
MAX_EXPORTS = 100
DOWNLOADS = 4


def _members(corpus: Path, limit: int) -> dict[str, list[int]]:
    """Dump members of each catalogued GRCh37 export revision."""
    revisions = {
        r.sites_sha256: r.id
        for r in load_catalogue()
        if r.kind != "composite" and r.exports and r.build == "GRCh37"
    }
    found: dict[str, list[int]] = collections.defaultdict(list)
    for line in (corpus / "fingerprints.jsonl").open():
        record = json.loads(line)
        # The revision's own coordinates: an export of the same marker list on
        # another build differs in positions and orientation.
        revision = revisions.get(record.get("sites_sha256", ""))
        if revision:
            found[revision].append(record["member"])
    return {k: sorted(v)[:limit] for k, v in sorted(found.items())}


def _count(text: io.StringIO, counts: dict[tuple[str, str, str], list[int]]) -> None:
    """Add one export's alleles: [called, het, A, C, G, T] per marker."""
    for line in text:
        if not line.strip() or line[0] == "#":
            continue
        fields = [
            f.strip()
            for f in line.replace('"', "").split("\t" if "\t" in line else ",")
        ]
        if len(fields) < 4 or fields[0].lower() in _COLUMN_LABELS:
            continue
        key = (fields[0], vendor_chrom(fields[1]), fields[2])
        rest = fields[3:]
        if len(rest) >= 2 and len(rest[0]) == len(rest[1]) == 1:
            call = rest[0] + rest[1]  # AncestryDNA: one allele per column
        else:
            call = rest[0]
        alleles = [a for a in call.upper() if a in BASES]
        if not alleles or len(alleles) != len(call):
            continue
        row = counts.get(key)
        if row is None:
            row = counts[key] = [0, 0, 0, 0, 0, 0]
        row[0] += 1
        row[1] += len(set(alleles)) > 1
        for allele in alleles:
            row[2 + BASES.index(allele)] += 1


def alleles(workdir: Path, corpus: Path, limit: int) -> None:
    out_dir = workdir / "alleles"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        rev: members
        for rev, members in _members(corpus, limit).items()
        if not (out_dir / f"{rev}.tsv.gz").exists()
    }
    local = threading.local()

    def read(index: int) -> bytes | None:
        # One HTTP range reader per download thread.
        if not hasattr(local, "archive"):
            local.archive = zipfile.ZipFile(
                io.BufferedReader(_HttpRange(DUMP_URL), 32 << 20)
            )
            local.infos = local.archive.infolist()
        info = local.infos[index]
        if info.file_size > MAX_MEMBER_BYTES:
            return None
        try:
            return bytes(local.archive.read(info))
        except (zipfile.BadZipFile, zlib.error, EOFError):
            return None

    with concurrent.futures.ThreadPoolExecutor(DOWNLOADS) as pool:
        for revision, members in plan.items():
            _aggregate(out_dir, revision, pool.map(read, members))


def _aggregate(out_dir: Path, revision: str, payloads: Iterable[bytes | None]) -> None:
    counts: dict[tuple[str, str, str], list[int]] = {}
    exports = 0
    for data in payloads:
        if data is None:
            continue
        try:
            _count(_member_text(data), counts)  # type: ignore[arg-type]
        except (UnicodeDecodeError, ValueError, zipfile.BadZipFile) as error:
            print(f"{revision}: skipped a member ({error})", file=sys.stderr)
            continue
        exports += 1
    path = out_dir / f"{revision}.tsv.gz"
    tmp = path.with_suffix(".partial")
    with gzip.GzipFile(tmp, "wb", mtime=0) as out:
        out.write(f"#exports\t{exports}\n".encode())
        out.write(b"#id\tchrom\tpos\tcalled\thet\tA\tC\tG\tT\n")
        for (marker, chrom, pos), row in sorted(counts.items()):
            values = "\t".join(map(str, row))
            out.write(f"{marker}\t{chrom}\t{pos}\t{values}\n".encode())
    tmp.rename(path)
    print(f"{revision}: {exports} exports, {len(counts)} markers", flush=True)


# Evidence thresholds: calls per revision at the marker, and the largest
# difference in reported-reference-allele frequency between a reverse report
# and the forward report confirming it.
MIN_CALLS = 10
MAX_FREQUENCY_GAP = 0.25
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def _read_counts(path: Path) -> Iterator[tuple[str, str, int, list[int]]]:
    with gzip.open(path, "rt") as source:
        for line in source:
            if line[0] == "#":
                continue
            marker, chrom, pos, called, _het, *bases = line.split("\t")
            if pos.isdigit() and int(pos) > 0:
                yield marker, chrom, int(pos), [int(called), *map(int, bases)]


def _exports(path: Path) -> int:
    with gzip.open(path, "rt") as source:
        return int(source.readline().split("\t")[1])


class Observation(NamedTuple):
    revision: str
    marker: str
    alleles: str  # sorted, as reported
    calls: int
    orientation: str  # forward, reverse or unknown
    reference_share: float  # of the reported alleles, the reference's strand


def _observe(revision: str, marker: str, ref: str, row: list[int]) -> Observation:
    calls, counts = row[0], row[1:]
    alleles = "".join(b for b, n in zip(BASES, counts, strict=True) if n)
    total = sum(counts)
    flipped = ref.translate(COMPLEMENT)
    orientation = "unknown"
    share = 0.0
    # Monomorphic reports cannot separate a reverse strand from a fixed
    # alternate allele, and A/T or C/G pairs read the same on both strands.
    if len(alleles) == 2 and alleles.translate(COMPLEMENT) not in {
        alleles,
        alleles[::-1],
    }:
        if ref in alleles:
            orientation, share = "forward", counts[BASES.index(ref)] / total
        elif flipped in alleles:
            orientation, share = "reverse", counts[BASES.index(flipped)] / total
    return Observation(revision, marker, alleles, calls, orientation, share)


def _reference_bases(
    reference: Path, sites: dict[str, set[int]]
) -> dict[tuple[str, int], str]:
    import pysam

    bases: dict[tuple[str, int], str] = {}
    with pysam.FastaFile(str(reference)) as fasta:
        for chrom, positions in sites.items():
            if chrom not in fasta.references:
                continue
            sequence = fasta.fetch(chrom).upper()
            for pos in positions:
                if pos <= len(sequence) and sequence[pos - 1] in BASES:
                    bases[(chrom, pos)] = sequence[pos - 1]
    return bases


def _inverted_since_ncbi36(chain: Path) -> dict[str, list[tuple[int, int]]]:
    """GRCh37 intervals (1-based, inclusive) that hg18ToHg19 maps from the other
    strand of NCBI36."""
    intervals: dict[str, list[tuple[int, int]]] = collections.defaultdict(list)
    with gzip.open(chain, "rt") as source:
        for line in source:
            if line.startswith("chain"):
                *_, q_name, q_size, q_strand, q_start, q_end, _ = line.split()
                if q_strand == "-":
                    intervals[vendor_chrom(q_name.removeprefix("chr"))].append(
                        (int(q_size) - int(q_end) + 1, int(q_size) - int(q_start))
                    )
    return intervals


def _period_start(revision: Revision) -> str | None:
    return revision.period.split("/")[0] if revision.period else None


def _template_sites(name: str, roots: list[str]) -> set[tuple[str, str, int]]:
    info = get_template_format(name)
    if info is None:
        return set()
    try:
        _, bodies = _resolve_templates_for_format(roots, name, info)
    except WGSExtractError:
        return set()
    return {
        (marker, vendor_chrom(chrom), int(pos))
        for body in bodies
        for marker, chrom, pos in template_rows(name, body)
        if pos.isdigit()
    }


def build(
    workdir: Path,
    reference: Path,
    roots: list[str],
    output: Path,
    chain: Path | None = None,
) -> None:
    catalogue = {r.id: r for r in load_catalogue()}
    files = {
        path.name.removesuffix(".tsv.gz"): path
        for path in sorted((workdir / "alleles").glob("*.tsv.gz"))
    }
    exports = {revision: _exports(path) for revision, path in files.items()}

    # Pass 1: reference bases at every reported site.
    sites: dict[str, set[int]] = collections.defaultdict(set)
    for path in files.values():
        for _, chrom, pos, _ in _read_counts(path):
            sites[chrom].add(pos)
    reference_bases = _reference_bases(reference, sites)
    del sites

    # Pass 2: sites some revision reported on the reverse strand.
    candidates: set[tuple[str, int]] = set()
    for revision, path in files.items():
        for marker, chrom, pos, row in _read_counts(path):
            ref = reference_bases.get((chrom, pos))
            if ref and row[0] >= MIN_CALLS:
                report = _observe(revision, marker, ref, row)
                if report.orientation == "reverse" and len(report.alleles) == 2:
                    candidates.add((chrom, pos))

    # Pass 3: every revision's report at those sites.
    observed: dict[tuple[str, int], list[Observation]] = collections.defaultdict(list)
    for revision, path in files.items():
        for marker, chrom, pos, row in _read_counts(path):
            if (chrom, pos) in candidates:
                ref = reference_bases[(chrom, pos)]
                observed[(chrom, pos)].append(_observe(revision, marker, ref, row))

    with gzip.open(workdir / "candidates.tsv.gz", "wt") as dump:
        dump.write("#chrom\tpos\tref\trevision\tid\talleles\tcalls\tstrand\tshare\n")
        for (chrom, pos), seen in sorted(observed.items()):
            for o in seen:
                dump.write(
                    f"{chrom}\t{pos}\t{reference_bases[(chrom, pos)]}\t{o.revision}"
                    f"\t{o.marker}\t{o.alleles}\t{o.calls}\t{o.orientation}"
                    f"\t{o.reference_share:.3f}\n"
                )

    quirks = []
    for (chrom, pos), seen in observed.items():
        for obs in seen:
            if not (
                obs.orientation == "reverse"
                and len(obs.alleles) == 2
                and obs.calls >= MIN_CALLS
            ):
                continue
            forward = "".join(sorted(obs.alleles.translate(COMPLEMENT)))
            confirming = [
                other
                for other in seen
                if other.orientation == "forward"
                and other.alleles == forward
                and other.calls >= MIN_CALLS
                and abs(other.reference_share - obs.reference_share)
                <= MAX_FREQUENCY_GAP
            ]
            if not confirming:
                continue
            quirks.append((obs, chrom, pos, forward, confirming))

    # A template reproduces a quirk only when every revision it stands for
    # (its exact revision, else the public revisions closest to it) that
    # reported the marker with a readable strand reported it reversed.
    reversed_at = {(q[0].revision, q[0].marker, q[1], q[2]) for q in quirks}
    applies: dict[tuple[str, str, str, int], list[str]] = collections.defaultdict(list)
    for name in sorted({r.template for r in catalogue.values() if r.template}):
        stands_for = [
            r.id
            for r in catalogue.values()
            if r.template == name
            and r.id in files
            and r.template_relation in {"exact", "same-markers"}
        ] or [
            r.id
            for r in catalogue.values()
            if r.template == name and r.id in files and r.template_relation == "closest"
        ]
        if not stands_for:
            continue
        template_sites = _template_sites(name, roots)
        for (chrom, pos), seen in observed.items():
            for marker in {o.marker for o in seen if o.revision in stands_for}:
                if (marker, chrom, pos) not in template_sites:
                    continue
                reports = [
                    o
                    for o in seen
                    if o.revision in stands_for
                    and o.marker == marker
                    and o.orientation != "unknown"
                ]
                # Too few calls to confirm a reverse report is no evidence
                # either way; any forward report is evidence against.
                confirmed = [
                    o
                    for o in reports
                    if (o.revision, marker, chrom, pos) in reversed_at
                ]
                if confirmed and all(
                    o in confirmed
                    or (o.orientation == "reverse" and o.calls < MIN_CALLS)
                    for o in reports
                ):
                    for o in confirmed:
                        applies[(o.revision, marker, chrom, pos)].append(name)

    rows = []
    for obs, chrom, pos, forward, confirming in quirks:
        revision = catalogue[obs.revision]
        start = _period_start(revision)
        fixed = sorted(
            (_period_start(catalogue[o.revision]) or "", o.revision)
            for o in confirming
            if o.marker == obs.marker
            and catalogue[o.revision].vendor == revision.vendor
            and start
            and (_period_start(catalogue[o.revision]) or "") > start
        )
        rows.append(
            (
                obs.revision,
                obs.marker,
                chrom,
                str(pos),
                obs.alleles,
                forward,
                fixed[0][1] if fixed else ".",
                ",".join(applies.get((obs.revision, obs.marker, chrom, pos), []))
                or ".",
                str(obs.calls),
                ",".join(sorted({o.revision for o in confirming})),
            )
        )
    rows.sort(key=lambda r: (r[0], chr_to_int(r[2]), int(r[3]), r[1]))
    with output.open("w", encoding="utf-8", newline="\n") as out:
        out.write(
            "# Markers that public vendor exports reported on the reverse strand of "
            "GRCh37.\n# Sources, columns and selection rules: README.md (strand.tsv)."
            "\n#revision\tid\tchrom\tposition\treported\tforward\tfixed_in\ttemplates"
            "\texports\tconfirmed_by\n"
        )
        out.writelines("\t".join(row) + "\n" for row in rows)
    summary = {
        "revisions": {r: exports[r] for r in files},
        "candidate_sites": len(candidates),
        "rows": len(rows),
        "rows_per_revision": collections.Counter(r[0] for r in rows),
        "fixed_later": sum(r[6] != "." for r in rows),
        "templates": collections.Counter(
            t for r in rows for t in r[7].split(",") if t != "."
        ),
    }
    if chain:
        inverted = _inverted_since_ncbi36(chain)
        summary["inverted_since_ncbi36"] = collections.Counter(
            r[0]
            for r in rows
            if any(lo <= int(r[3]) <= hi for lo, hi in inverted.get(r[2], ()))
        )
    (workdir / "build-summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    cmd = sub.add_parser("alleles")
    cmd.add_argument("workdir", type=Path)
    cmd.add_argument("--corpus", type=Path, required=True)
    cmd.add_argument("--max-exports", type=int, default=MAX_EXPORTS)
    cmd = sub.add_parser("build")
    cmd.add_argument("workdir", type=Path)
    cmd.add_argument("--reference", type=Path, required=True)
    cmd.add_argument("--templates", action="append", default=[])
    cmd.add_argument("--output", type=Path, default=STRAND_TABLE)
    cmd.add_argument("--ncbi36-chain", type=Path, help="hg18ToHg19.over.chain.gz")
    args = parser.parse_args()
    if args.cmd == "alleles":
        alleles(args.workdir, args.corpus, args.max_exports)
    else:
        packaged = str(CATALOGUE.parents[1] / "raw_file_templates")
        build(
            args.workdir,
            args.reference,
            [*args.templates, packaged],
            args.output,
            args.ncbi36_chain,
        )


if __name__ == "__main__":
    main()
