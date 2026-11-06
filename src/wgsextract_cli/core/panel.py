"""``panel bed``: genes and presets to a padded, merged BED for adaptive sampling.

Gene spans come from the bundled NCBI RefSeq annotations
(assets/panel/genes_<build>.tsv.gz, made by scripts/build_panel_annotations.py);
nothing is fetched at run time.
"""

from __future__ import annotations

import difflib
import gzip
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path

from wgsextract_cli.core.panel_presets import PRESETS, preset_genes
from wgsextract_cli.core.utils import UsageError

BUILDS = ("hg38", "t2t", "hg19")
# ONT: adaptive sampling enriches best below about 5% of the genome.
WARN_FRACTION = 0.05
RECOMMENDED_FRACTION = 0.01


@dataclass(frozen=True)
class Locus:
    chrom: str
    start: int  # 0-based
    end: int  # exclusive
    symbol: str


@dataclass(frozen=True)
class Annotations:
    build: str
    source: str
    contigs: Mapping[str, int]
    loci: Mapping[str, tuple[Locus, ...]]  # by upper-case symbol
    aliases: Mapping[str, tuple[str, ...]]  # upper-case synonym -> symbols


@cache
def load_annotations(build: str) -> Annotations:
    resource = resources.files("wgsextract_cli").joinpath(
        f"assets/panel/genes_{build}.tsv.gz"
    )
    contigs: dict[str, int] = {}
    loci: dict[str, list[Locus]] = {}
    aliases: dict[str, set[str]] = {}
    source = ""
    with gzip.open(io.BytesIO(resource.read_bytes()), "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#contig\t"):
                _, name, length = line.split("\t")
                contigs[name] = int(length)
            elif line.startswith("# Genes"):
                # "... annotation GCF_000001405.40-RS_2025_08 (...)"
                source = f"NCBI RefSeq {line.split('annotation ', 1)[1].split()[0]}"
            elif not line.startswith(("#", "chrom\t")):
                chrom, start, end, symbol, _, _, synonyms = line.rstrip("\n").split(
                    "\t"
                )
                locus = Locus(chrom, int(start), int(end), symbol)
                loci.setdefault(symbol.upper(), []).append(locus)
                for alias in filter(None, synonyms.split(",")):
                    aliases.setdefault(alias.upper(), set()).add(symbol)
    return Annotations(
        build,
        source,
        contigs,
        {k: tuple(v) for k, v in loci.items()},
        {k: tuple(sorted(v)) for k, v in aliases.items() if k not in loci},
    )


@dataclass
class Resolution:
    loci: list[Locus] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # given -> symbol


def resolve_genes(names: Iterable[str], notes: Annotations) -> Resolution:
    """Symbols (any case) or unique aliases to loci; one error lists every
    name that is unknown or an alias of several genes."""
    found = Resolution()
    problems: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.upper()
        symbols: tuple[str, ...] = (key,) if key in notes.loci else ()
        if not symbols:
            candidates = notes.aliases.get(key, ())
            if len(candidates) == 1:
                symbols = (candidates[0].upper(),)
                found.aliases[name] = candidates[0]
            elif candidates:
                problems.append(
                    f"{name}: an alias of several genes ({', '.join(candidates)}); "
                    "give the symbol"
                )
                continue
            else:
                close = difflib.get_close_matches(key, notes.loci, n=3, cutoff=0.8)
                close += sorted(s for s in notes.loci if s.startswith(key + "-"))[:5]
                hint = (
                    f" (did you mean {', '.join(notes.loci[c][0].symbol for c in close)}?)"
                    if close
                    else ""
                )
                problems.append(f"{name}: not a gene in {notes.source}{hint}")
                continue
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                found.loci.extend(notes.loci[symbol])
    if problems:
        raise UsageError("Unknown genes:\n  " + "\n  ".join(problems))
    return found


def span_locus(name: str, first: str, last: str, notes: Annotations) -> Locus:
    """One locus from the start of ``first`` to the end of ``last``."""
    a, b = (resolve_genes([gene], notes).loci for gene in (first, last))
    chrom = a[0].chrom
    if {locus.chrom for locus in a + b} != {chrom}:
        raise UsageError(f"{name}: {first} and {last} are on different contigs.")
    return Locus(chrom, min(x.start for x in a), max(x.end for x in b), name)


def read_gene_file(path: Path) -> list[str]:
    """Symbols separated by newlines, commas or white space; # starts a comment."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise UsageError(f"Cannot read --gene-file {path}: {exc}") from exc
    return [
        word
        for line in text.splitlines()
        for word in line.split("#", 1)[0].replace(",", " ").split()
    ]


def reference_contigs(
    fai_lengths: Mapping[str, int], notes: Annotations
) -> tuple[dict[str, str], list[str]]:
    """Map the annotation's chrN names to the reference's (chr1 or 1, chrM or
    MT); return the map and the contigs whose lengths differ."""
    names: dict[str, str] = {}
    differ: list[str] = []
    for chrom, length in notes.contigs.items():
        bare = chrom.removeprefix("chr")
        options = ["chrM", "MT", "chrMT", "M"] if bare == "M" else [chrom, bare]
        match = next((n for n in options if n in fai_lengths), None)
        if match is None:
            continue
        names[chrom] = match
        if fai_lengths[match] != length:
            differ.append(chrom)
    return names, differ


@dataclass(frozen=True)
class Target:
    chrom: str
    start: int
    end: int
    names: tuple[str, ...]


def build_targets(
    loci: Sequence[Locus], pad: int, lengths: Mapping[str, int]
) -> list[Target]:
    """Pad each locus, clip it to its contig and merge overlapping or touching
    ones, in the contigs' order."""
    order = {name: i for i, name in enumerate(lengths)}
    padded = sorted(
        (
            (
                order[x.chrom],
                max(0, x.start - pad),
                min(lengths[x.chrom], x.end + pad),
                x,
            )
            for x in loci
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    merged: list[Target] = []
    for _, start, end, locus in padded:
        last = merged[-1] if merged else None
        if last and last.chrom == locus.chrom and start <= last.end:
            names = last.names + (() if locus.symbol in last.names else (locus.symbol,))
            merged[-1] = Target(last.chrom, last.start, max(last.end, end), names)
        else:
            merged.append(Target(locus.chrom, start, end, (locus.symbol,)))
    return merged


def bed_text(targets: Iterable[Target], names: Mapping[str, str]) -> str:
    """BED4 (no strand, so MinKNOW accepts reads of both strands)."""
    return "".join(
        f"{names.get(t.chrom, t.chrom)}\t{t.start}\t{t.end}\t{','.join(t.names)}\n"
        for t in targets
    )


def known_presets() -> tuple[str, ...]:
    return tuple(PRESETS)


def preset_loci(name: str, notes: Annotations) -> list[Locus]:
    preset = PRESETS[name]
    loci = resolve_genes(preset_genes(preset), notes).loci
    loci += [span_locus(s.name, s.first, s.last, notes) for s in preset.spans]
    return loci
