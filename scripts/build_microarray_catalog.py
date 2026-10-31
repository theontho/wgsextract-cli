"""Reproduce the chain-exact GRCh37/38 microarray coordinate catalogs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import sys
from array import array
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pyliftover import LiftOver

from wgsextract_cli.core.microarray_aadr import _MARKERS, _chrom_name, aadr_markers
from wgsextract_cli.core.microarray_catalog import _MAGIC, _Catalog
from wgsextract_cli.core.microarray_targets import (
    _FORMATS,
    _template_paths,
)
from wgsextract_cli.core.microarray_utils import (
    _PACKAGED_TEMPLATES,
    _chrom_for_liftover,
    _resolve_templates_roots,
    ncbi36_to_grch37,
    template_rows,
)

Site = tuple[str, int]
CHAIN_URLS = {
    f"{source}To{target}.over.chain.gz": (
        f"https://hgdownload.soe.ucsc.edu/goldenPath/{source}/liftOver/"
        f"{source}To{target}.over.chain.gz"
    )
    for source, target in (("hg18", "Hg19"), ("hg19", "Hg38"), ("hg38", "Hg19"))
}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def generator_provenance() -> dict[str, object]:
    return {
        "script": "scripts/build_microarray_catalog.py",
        "sha256": _digest(Path(__file__)),
        "python": sys.version.split()[0],
        "pyliftover": importlib.metadata.version("pyliftover"),
        "encoding": "v3: gzip, byte-lane shuffled little-endian delta columns",
        "command": (
            "pixi run -e dev python scripts/build_microarray_catalog.py "
            "--reference-library REFERENCE --output OUTPUT"
        ),
    }


def _integer(value: int) -> bytes:
    if not 0 <= value < 1 << 64:
        raise ValueError(f"Invalid unsigned integer: {value}")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def build_map(
    sites: Iterable[Site],
    chain: Path,
    destination: Path,
    projected: Callable[[str, int, list[Any]], None] | None = None,
) -> dict[str, Any]:
    """Keep the original zero-based query and every hit, including its score."""
    ordered = sorted(set(sites))
    lifter = LiftOver(str(chain))
    chroms = {chrom: code for code, chrom in enumerate(sorted({c for c, _ in ordered}))}
    keys, hit_counts = array("Q"), array("I")
    destination_deltas, scores, reverse = array("q"), array("Q"), array("B")
    counts: Counter[str] = Counter()
    destinations: Counter[Site] = Counter()
    previous_key = previous_destination = 0
    for chrom, position in ordered:
        key = chroms[chrom] << 32 | position
        keys.append(key - previous_key)
        previous_key = key
        hits = lifter.convert_coordinate(chrom, position) or []
        hit_counts.append(len(hits))
        counts["rows"] += 1
        counts["unmapped"] += not hits
        counts["multiple"] += len(hits) > 1
        for target, target_position, strand, score in hits:
            code = chroms.setdefault(target, len(chroms))
            destination_key = code << 32 | target_position
            destination_deltas.append(destination_key - previous_destination)
            previous_destination = destination_key
            reverse.append(strand == "-")
            scores.append(score)
            destinations[(target, target_position)] += 1
            counts["placements"] += 1
            counts["inverted"] += strand == "-"
            counts["nonprimary"] += "_" in target
        if projected:
            projected(chrom, position, hits)
    header = _MAGIC + _integer(len(ordered)) + _integer(len(chroms))
    for chrom in chroms:
        encoded = chrom.encode("utf-8")
        header += _integer(len(encoded)) + encoded
    header += _integer(len(scores))
    columns = (keys, hit_counts, destination_deltas, scores, reverse)
    if sys.byteorder != "little":
        for column in columns:
            column.byteswap()
    # Byte lanes keep the mostly zero high delta bytes adjacent, retaining
    # fast array decoding without doubling the varint prototype's asset size.
    shuffled = []
    for column in columns:
        encoded = column.tobytes()
        shuffled.append(
            b"".join(
                encoded[lane :: column.itemsize] for lane in range(column.itemsize)
            )
        )
    payload = header + b"".join(shuffled)
    compressed = gzip.compress(payload, mtime=0)
    destination.write_bytes(compressed)
    # This exhaustive check is independent of the catalog's encoder. It proves
    # all placements, scores, both strand directions and explicit unmapped rows.
    decoded = _Catalog(payload)
    for chrom, position in ordered:
        for strand in ("+", "-"):
            expected = lifter.convert_coordinate(chrom, position, strand) or []
            if decoded.lookup(chrom, position, strand) != expected:
                raise ValueError(f"Catalog/chain mismatch: {chrom}:{position} {strand}")
    counts["collision_destinations"] = sum(count > 1 for count in destinations.values())
    counts["collision_placements"] = sum(
        count for count in destinations.values() if count > 1
    )
    return {
        "filename": destination.name,
        "chain": chain.name,
        "source_url": CHAIN_URLS.get(chain.name),
        "sha256": hashlib.sha256(compressed).hexdigest(),
        "decoded_sha256": hashlib.sha256(payload).hexdigest(),
        "compressed_bytes": len(compressed),
        "decoded_bytes": len(payload),
        "rows": len(ordered),
        "counts": dict(counts),
        "exhaustive_both_strands_chain_parity": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    roots = _resolve_templates_roots(
        [str(args.reference_library), str(_PACKAGED_TEMPLATES)]
    )
    paths = list(_template_paths(roots))
    found = {name for name, _ancient, _parts in paths}
    if found != set(_FORMATS):
        raise ValueError(f"Missing layouts: {sorted(set(_FORMATS) - found)}")
    chain36 = args.reference_library / "hg18ToHg19.over.chain.gz"
    chain38 = args.reference_library / "hg19ToHg38.over.chain.gz"
    reverse_chain = args.reference_library / "hg38ToHg19.over.chain.gz"
    source37: set[Site] = set()
    source36: set[Site] = set()
    lifter36 = LiftOver(str(chain36))
    templates: dict[str, object] = {}
    for name, ancient, parts in paths:
        templates[name] = [
            {"part": index + 1, "sha256": _digest(Path(path))}
            for index, path in enumerate(parts)
        ]
        for path in parts:
            for _rsid, chrom, pos in template_rows(name, path):
                if pos.isdigit() and int(pos) > 0 and chrom:
                    if ancient:
                        source36.add((_chrom_for_liftover(chrom), int(pos) - 1))
                        lifted = ncbi36_to_grch37(lifter36, chrom, pos)
                        if lifted is None:
                            continue
                        chrom, pos, _inverted = lifted
                    source37.add((_chrom_for_liftover(chrom), int(pos) - 1))
    legacy_count = len(source37)
    for marker in aadr_markers("1240k"):
        if marker.position > 0:
            source37.add(
                (_chrom_for_liftover(_chrom_name(marker.chrom)), marker.position - 1)
            )
    source38: set[Site] = set()

    def retain_export_sites(chrom: str, position: int, hits: list[Any]) -> None:
        # Preserve the target builder's mitochondrial identity policy. Export
        # of those same sites deliberately uses the historic reverse chain.
        if chrom == "chrM":
            source38.add((chrom, position))
        if hits and "_" not in hits[0][0]:
            source38.add((hits[0][0], hits[0][1]))

    maps = {}
    for name, sites, chain, callback in (
        ("hg18-to-hg19", source36, chain36, None),
        ("hg19-to-hg38", source37, chain38, retain_export_sites),
        ("hg38-to-hg19", source38, reverse_chain, None),
    ):
        spec = build_map(sites, chain, args.output / f"{name}.bin.gz", callback)
        maps[_digest(chain)] = spec
        print(name, json.dumps(spec, sort_keys=True), flush=True)
    manifest = {
        "format": "wgsextract-legacy-coordinate-catalog-v3",
        "source": "Existing ordered consumer templates plus AADR v66.p1",
        "generator": generator_provenance(),
        "policy": {
            "coordinates": "zero-based; all placements in original chain order",
            "ids_and_order": "authoritative templates, not duplicated in this asset",
            "ambiguity": "unfiltered static graph; existing caller policy unchanged",
            "mitochondria": "hg38 target rCRS identity; reverse export UCSC Yoruba chain",
            "custom_queries": "original chain, loaded lazily",
            "ncbi36_without_chain": "existing sample-dependent rsID lookup unchanged",
        },
        "legacy_grch37_sites": legacy_count,
        "complete_grch37_sites": len(source37),
        "templates": templates,
        "corrections_sha256": _digest(_PACKAGED_TEMPLATES / "corrections.tsv"),
        "aadr_sha256": _digest(_MARKERS),
        "maps": maps,
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
