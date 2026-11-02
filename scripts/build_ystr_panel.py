"""Build the bundled Y-STR marker panel (src/wgsextract_cli/ydna/ystr/).

Usage (from the repository root):

    pixi run -e dev python scripts/build_ystr_panel.py \
        --hg38 GRCh38.fa.gz --hg19 GRCh37.fa.gz --t2t chm13v2.0.fa.gz \
        --cache tmp/ystr-sources

The FASTAs need a samtools faidx index. Sources are downloaded into --cache,
pinned by commit/SHA-256 (see SOURCES); GenBank BACs come from NCBI E-utilities.

For each marker the script

1. takes the repeat's GRCh38 position from the first source that has it:
   NIST STRBase, then HipSTR-references, then Y-LineageTracker, then a
   Y-STR_Kit GRCh37 window searched for the marker's repeat;
2. widens that core until the KMER bases on each side occur once in the
   surrounding sequence and can't match an expanded repeat, so a read's tract
   allele is the bases between the two flanks;
3. finds the same tract in GRCh37 and CHM13 by its flanking sequence;
4. sets the reference allele (GRCh38's count in FamilyTreeDNA's convention)
   from NIST's reference allele, carried from NIST's GenBank BAC to GRCh38 by
   the difference in tract length, plus FamilyTreeDNA's documented offset; or,
   for markers NIST doesn't list, the repeat units in the core.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

KMER = 14
SEARCH_PAD = 60
MAX_WIDEN = 120
CONTEXT = 40
ANCHOR_SHIFTS = (0, 8, 20, 40, 80)

_HIPSTR = "https://raw.githubusercontent.com/HipSTR-Tool/HipSTR-references/a396cd453c3ad6e5df6415f6a596fde56f20085c/human"
_YLT = "https://raw.githubusercontent.com/Shuhua-Group/Y-LineageTracker/82b14c74be95ef2d4d929ce20bf7436869f163ea"
_YSTRKIT = "https://raw.githubusercontent.com/savolab/Y-STR_Kit/82c95cc68e890bc735c7e0657c34a60ee7136601"

SOURCES: dict[str, dict[str, str]] = {
    "hipstr_hg38": {
        "url": f"{_HIPSTR}/all_annot_markers.hg38.fixed.bed",
        "sha256": "d5b91757c7de756ada8a940cb5588e6f51aa79513ae6ce8cb4f1b9df63df9e61",
        "license": "MIT (HipSTR-Tool/HipSTR-references)",
        "cite": "Willems et al. 2016, Am J Hum Genet 98:919-933 (PMID 27126583)",
    },
    "strbase_assembly": {
        "url": "https://strbase.nist.gov/api/RelLociAssembly",
        "sha256": "",  # a live API; the response used is recorded in provenance
        "license": "Public domain (NIST, 17 U.S.C. 105)",
        "cite": "NIST STRBase, https://strbase.nist.gov",
    },
    "nist_ystr_fact": {
        "url": "https://web.archive.org/web/20201208195221id_/https://strbase.nist.gov/ystr_fact.htm",
        "sha256": "3ccdd31e4c3378d9dc2cfc763a38fcf44e2f88e8c713ec26fe7ea85273092703",
        "license": "Public domain (NIST, 17 U.S.C. 105)",
        "cite": "NIST STRBase Y-STR fact sheet (reference alleles); Butler, Kline & Decker 2008, J Genet Geneal 4:125-148",
    },
    "ylt_panel": {
        "url": f"{_YLT}/LineageTracker/Data/Y_STR_panel.csv",
        "sha256": "6a210052be456a31b507f6e529c7d74f097638a4d45567cd06e867b0142d5fe3",
        "license": "MIT (Shuhua-Group/Y-LineageTracker)",
        "cite": "Chen et al. 2021, BMC Bioinformatics 22:557",
    },
    "ystrkit_info": {
        "url": f"{_YSTRKIT}/ref/ystr_info.txt",
        "sha256": "dc403aa48304c401422f7da2e46f2475c9fd057c789ba0bbc8f9ef303fc577cd",
        "license": "README states MIT, but the repository has no LICENSE file; "
        "only its GRCh37 windows and regexes are used, to locate repeats in "
        "the references and count their units. No Y-STR_Kit file is bundled",
        "cite": "Y-STR Kit, https://github.com/savolab/Y-STR_Kit",
    },
}

# FamilyTreeDNA's Y-111 order (Y-STR Results Guide), then forensic-kit markers
# outside it. Columns: marker, FTDNA order, smallest FTDNA panel, forensic kits,
# repeat unit length, kind, GRCh38 cores ("name" of the source row, ';' between
# summed parts), FTDNA count minus NIST count, note.
# kind: single | sum (parts counted together) | multicopy (copies sorted) | none
PPY23 = "PPY23"
YFP = "YfilerPlus"
SPEC = """
DYS393	1	Y-12	PPY23,YfilerPlus	4	single	DYS393	0
DYS390	2	Y-12	PPY23,YfilerPlus	4	single	DYS390	0
DYS19	3	Y-12	PPY23,YfilerPlus	4	single	DYS19	0
DYS391	4	Y-12	PPY23,YfilerPlus	4	single	DYS391	0
DYS385a	5	Y-12	PPY23,YfilerPlus	4	multicopy	DYS385ab_a;DYS385ab_b	0	Two copies; values are sorted, as FTDNA and forensic kits report them.
DYS385b	6	Y-12	PPY23,YfilerPlus	4	multicopy	DYS385ab_a;DYS385ab_b	0	Two copies; values are sorted, as FTDNA and forensic kits report them.
DYS426	7	Y-12		3	single	DYS426	0
DYS388	8	Y-12		3	single	DYS388	0
DYS439	9	Y-12	PPY23,YfilerPlus	4	single	DYS439	0
DYS389I	10	Y-12	PPY23,YfilerPlus	4	single	DYS389I	0
DYS392	11	Y-12	PPY23,YfilerPlus	3	single	DYS392	0
DYS389II	12	Y-12	PPY23,YfilerPlus	4	sum	DYS389I;DYS389II.2	0	FTDNA and kits report the total, which includes DYS389I.
DYS458	13	Y-25	PPY23,YfilerPlus	4	single	DYS458	0
DYS459a	14	Y-25		4	multicopy	DYS459_1;DYS459_2	0	Two copies; values are sorted.
DYS459b	15	Y-25		4	multicopy	DYS459_1;DYS459_2	0	Two copies; values are sorted.
DYS455	16	Y-25		4	single	DYS455	0
DYS454	17	Y-25		4	single	DYS454	0
DYS447	18	Y-25		5	single	ylt:DYS447	0
DYS437	19	Y-25	PPY23,YfilerPlus	4	single	DYS437	0
DYS448	20	Y-25	PPY23,YfilerPlus	6	sum	DYS448.1;DYS448.2	0
DYS449	21	Y-25	YfilerPlus	4	sum	DYS449.1;DYS449.2	0
DYS464a	22	Y-25		4	none		0	Four or more copies in palindromes; not called.
DYS464b	23	Y-25		4	none		0	Four or more copies in palindromes; not called.
DYS464c	24	Y-25		4	none		0	Four or more copies in palindromes; not called.
DYS464d	25	Y-25		4	none		0	Four or more copies in palindromes; not called.
DYS460	26	Y-37	YfilerPlus	4	single	DYS460	0
Y-GATA-H4	27	Y-37	PPY23,YfilerPlus	4	single	YGATAH4	-1
YCAIIa	28	Y-37		2	multicopy	YCAII_1;YCAII_2	0	Two copies; values are sorted.
YCAIIb	29	Y-37		2	multicopy	YCAII_1;YCAII_2	0	Two copies; values are sorted.
DYS456	30	Y-37	PPY23,YfilerPlus	4	single	DYS456	0
DYS607	31	Y-37		4	single	DYS607	0
DYS576	32	Y-37	PPY23,YfilerPlus	4	single	DYS576	0
DYS570	33	Y-37	PPY23,YfilerPlus	4	single	DYS570	0
CDYa	34	Y-37		4	none		0	Two copies in palindrome P1, longer than short reads; not called.
CDYb	35	Y-37		4	none		0	Two copies in palindrome P1, longer than short reads; not called.
DYS442	36	Y-37		4	single	DYS442	-5
DYS438	37	Y-37	PPY23,YfilerPlus	5	single	DYS438	0
DYS531	38	Y-67		4	single	DYS531	0
DYS578	39	Y-67		4	single	DYS578	0
DYF395S1a	40	Y-67		3	multicopy	ylt:DYS395S1a;ylt:DYS395S1b	0	Two copies; values are sorted.
DYF395S1b	41	Y-67		3	multicopy	ylt:DYS395S1a;ylt:DYS395S1b	0	Two copies; values are sorted.
DYS590	42	Y-67		5	single	DYS590	0
DYS537	43	Y-67		4	single	DYS537	0
DYS641	44	Y-67		4	single	DYS641	0
DYS472	45	Y-67		3	single	DYS472	0
DYF406S1	46	Y-67		4	single	DYF406S1	0
DYS511	47	Y-67		4	single	DYS511	0
DYS425	48	Y-67		3	single	DYS425	0
DYS413a	49	Y-67		2	multicopy	DYS413_1;DYS413_2	0	Two copies; values are sorted.
DYS413b	50	Y-67		2	multicopy	DYS413_1;DYS413_2	0	Two copies; values are sorted.
DYS557	51	Y-67		4	single	DYS557	0
DYS594	52	Y-67		5	single	DYS594	0
DYS436	53	Y-67		3	single	DYS436	0
DYS490	54	Y-67		3	single	DYS490	0
DYS534	55	Y-67		4	single	DYS534	0
DYS450	56	Y-67		5	single	DYS450.1	0
DYS444	57	Y-67		4	single	DYS444	0
DYS481	58	Y-67	PPY23,YfilerPlus	3	single	DYS481	0
DYS520	59	Y-67		4	single	DYS520	0
DYS446	60	Y-67		5	single	DYS446	0
DYS617	61	Y-67		3	single	DYS617	0
DYS568	62	Y-67		4	single	DYS568	0
DYS487	63	Y-67		3	single	DYS487	0
DYS572	64	Y-67		4	single	DYS572	0
DYS640	65	Y-67		4	single	DYS640	0
DYS492	66	Y-67		3	single	DYS492	0
DYS565	67	Y-67		4	single	DYS565	0
DYS710	68	Y-111		4	single	ystrkit:DYS710	0
DYS485	69	Y-111		3	single	DYS485	0
DYS632	70	Y-111		4	single	DYS632	0
DYS495	71	Y-111		3	single	DYS495	0
DYS540	72	Y-111		4	single	DYS540	0
DYS714	73	Y-111		5	single	DYS714	0
DYS716	74	Y-111		5	single	ylt:DYS716	0
DYS717	75	Y-111		5	single	DYS717	0
DYS505	76	Y-111		4	single	DYS505	0
DYS556	77	Y-111		4	single	DYS556	0
DYS549	78	Y-111	PPY23	4	single	DYS549	0
DYS589	79	Y-111		5	single	DYS589	0
DYS522	80	Y-111		4	single	DYS522	0
DYS494	81	Y-111		3	single	DYS494	0
DYS533	82	Y-111	PPY23,YfilerPlus	4	single	DYS533	0
DYS636	83	Y-111		4	single	DYS636	0
DYS575	84	Y-111		4	single	DYS575	0
DYS638	85	Y-111		4	single	DYS638	0
DYS462	86	Y-111		4	single	DYS462	0
DYS452	87	Y-111		5	single	DYS452	0
DYS445	88	Y-111		4	single	DYS445	0
Y-GATA-A10	89	Y-111		4	single	Y-GATA-A10	-2
DYS463	90	Y-111		5	single	DYS463	0
DYS441	91	Y-111		4	single	ylt:DYS441	-1
Y-GGAAT-1B07	92	Y-111		5	single	ystrkit:Y-GGAAT-1B07	0
DYS525	93	Y-111		4	single	DYS525	0
DYS712	94	Y-111		4	single	ystrkit:DYS712	0
DYS593	95	Y-111		5	single	DYS593	0
DYS650	96	Y-111		4	single	ystrkit:DYS650	0
DYS532	97	Y-111		4	sum	DYS532.1;DYS532.2	0
DYS715	98	Y-111		4	single	ystrkit:DYS715	0
DYS504	99	Y-111		4	sum	DYS504.1;DYS504.2	0
DYS513	100	Y-111		4	single	DYS513	0
DYS561	101	Y-111		4	single	DYS561	0
DYS552	102	Y-111		4	sum	DYS552.1;DYS552.2	0
DYS726	103	Y-111		4	single	ystrkit:DYS726	0
DYS635	104	Y-111	PPY23,YfilerPlus	4	single	DYS635	0
DYS587	105	Y-111		5	single	DYS587	0
DYS643	106	Y-111	PPY23	5	single	DYS643	0
DYS497	107	Y-111		3	single	DYS497	0
DYS510	108	Y-111		4	sum	DYS510.1;DYS510.2;DYS510.3	0
DYS434	109	Y-111		4	single	DYS434	0
DYS461	110	Y-111		4	single	DYS461	0
DYS435	111	Y-111		4	single	DYS435	0
DYS518			YfilerPlus	4	sum	DYS518.1;DYS518.2	0
DYS627			YfilerPlus	4	single	DYS627	0
DYF387S1			YfilerPlus	4	none		0	Two copies in palindrome P1, longer than short reads; not called.
"""

# GRCh38 values (NIST convention) that 1000 Genomes PowerPlex Y23 typings
# contradict NIST's GenBank allele for: every 1000 Genomes sample's call
# differed from its PowerPlex Y23 typing by the same number of repeats.
CALIBRATED = {
    "DYS448": (19, "1000 Genomes PowerPlex Y23 typings (NIST lists 22 for AC025227)"),
    "DYS481": (22, "1000 Genomes PowerPlex Y23 typings"),
}

# NIST's names where they differ from the panel's.
NIST_NAMES = {
    "DYS385a": "DYS385 a/b",
    "DYS385b": "DYS385 a/b",
    "DYS389I": "DYS389 I DYS389 II",
    "DYS389II": "DYS389 I DYS389 II",
    "DYS459a": "DYS459 a/b",
    "DYS459b": "DYS459 a/b",
    "YCAIIa": "YCAII a/b",
    "YCAIIb": "YCAII a/b",
    "DYS460": "DYS460 (A7.1)",
    "DYS461": "DYS461 (A7.2)",
    "DYS635": "DYS635 (C4)",
}
# STRBase RelLociAssembly locus names where they differ.
STRBASE_NAMES = {"Y-GATA-H4": "YGATAH4", "YGATAH4": "YGATAH4"}


@dataclass
class Core:
    start: int
    end: int
    source: str
    copies: float | None


def fetch(url: str, dest: Path, sha256: str | None) -> Path:
    if not dest.exists():
        print(f"downloading {url}", file=sys.stderr)
        request = urllib.request.Request(url, headers={"User-Agent": "wgsextract"})
        with urllib.request.urlopen(request, timeout=120) as response:
            dest.write_bytes(response.read())
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if sha256 and digest != sha256:
        raise SystemExit(f"{dest}: SHA-256 {digest} != pinned {sha256}")
    return dest


def chrom_seq(fasta: str, contig: str) -> str:
    out = subprocess.run(
        ["samtools", "faidx", fasta, contig],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return "".join(out.splitlines()[1:]).upper()


def y_contig(fasta: str) -> str:
    names = [
        line.split("\t")[0] for line in Path(fasta + ".fai").read_text().splitlines()
    ]
    for name in ("chrY", "Y"):
        if name in names:
            return name
    raise SystemExit(f"{fasta} has no chrY/Y")


def revcomp(seq: str) -> str:
    return seq[::-1].translate(str.maketrans("ACGTN", "TGCAN"))


def near_matches(seq: str, pattern: str, limit: int = 1) -> list[int]:
    k = len(pattern)
    hits = []
    for pos in range(len(seq) - k + 1):
        mismatches = 0
        for a, b in zip(seq[pos : pos + k], pattern, strict=True):
            if a != b:
                mismatches += 1
                if mismatches > limit:
                    break
        if mismatches <= limit:
            hits.append(pos)
    return hits


def tandem_sequences(core: str, period: int) -> list[str]:
    """The core's repeat units, each repeated: what an expanded allele reads like."""
    units = {core[i : i + period] for i in range(0, len(core) - period + 1)}
    return [unit * (KMER // period + 3) for unit in units if len(unit) == period]


def flank_ok(ref: str, pos: int, start: int, end: int, tandems: list[str]) -> bool:
    flank = ref[pos : pos + KMER]
    if "N" in flank:
        return False
    window = ref[start - KMER - SEARCH_PAD : end + KMER + SEARCH_PAD]
    if len(near_matches(window, flank)) != 1:
        return False
    return not any(near_matches(t, flank) for t in tandems)


class PanelError(Exception):
    pass


def widen(ref: str, core: Core, period: int) -> tuple[int, int]:
    """0-based half-open tract [s, e) whose KMER flanks are unique and not repeat-like."""
    s, e = core.start - 1, core.end
    tandems = tandem_sequences(ref[s:e], period)
    for _ in range(MAX_WIDEN):
        if flank_ok(ref, s - KMER, s, e, tandems):
            break
        s -= 1
    else:
        raise PanelError("no unique left flank within reach")
    for _ in range(MAX_WIDEN):
        if flank_ok(ref, e, s, e, tandems):
            break
        e += 1
    else:
        raise PanelError("no unique right flank within reach")
    return s, e


def unique_find(haystack: str, needle: str) -> int | None:
    first = haystack.find(needle)
    if first == -1 or haystack.find(needle, first + 1) != -1:
        return None
    return first


def mismatches(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b, strict=True)) + abs(len(a) - len(b))


def locate(ref38: str, s: int, e: int, other: str) -> tuple[int, int] | None:
    """The same tract in another assembly, by its flanking sequence.

    Anchors start at the tract and step outwards, so a variant next to the
    tract doesn't lose it; the KMER flanks found must then match GRCh38's to
    within one base.
    """
    flank_l, flank_r = ref38[s - KMER : s], ref38[e : e + KMER]
    for shift in ANCHOR_SHIFTS:
        for context in (CONTEXT, 30, 22):
            left = ref38[s - KMER - context - shift : s - shift]
            right = ref38[e + shift : e + KMER + context + shift]
            lpos = unique_find(other, left)
            rpos = unique_find(other, right)
            if lpos is None or rpos is None:
                continue
            bs, be = lpos + len(left) + shift, rpos - shift
            if (
                be >= bs
                and be - bs < (e - s) + 400
                and mismatches(other[bs - KMER : bs], flank_l) <= 1
                and mismatches(other[be : be + KMER], flank_r) <= 1
            ):
                return bs, be
    return None


def locate_in_bac(ref38: str, s: int, e: int, bac: str) -> int | None:
    """Tract length in a BAC (either strand), or None if not found."""
    for seq in (bac, revcomp(bac)):
        found = locate(ref38, s, e, seq)
        if found:
            return found[1] - found[0]
    return None


def parse_spec() -> list[dict[str, str]]:
    rows = []
    for line in SPEC.strip("\n").splitlines():
        cols = (line.split("\t") + [""] * 9)[:9]
        rows.append(
            dict(
                zip(
                    (
                        "marker",
                        "order",
                        "panel",
                        "kits",
                        "period",
                        "kind",
                        "cores",
                        "ftdna_offset",
                        "note",
                    ),
                    cols,
                    strict=True,
                )
            )
        )
    return rows


@dataclass
class Sources:
    hipstr: dict[str, Core]
    strbase: dict[str, Core]
    ylt: dict[str, Core]
    ystrkit: dict[str, tuple[int, int, str, str]]  # start, end, motif, regex
    nist: dict[str, tuple[str, list[int]]]  # BAC, reference alleles


def load_sources(cache: Path) -> Sources:
    cache.mkdir(parents=True, exist_ok=True)
    paths = {
        key: fetch(src["url"], cache / Path(src["url"]).name, src["sha256"] or None)
        for key, src in SOURCES.items()
        if key != "strbase_assembly"
    }
    strbase_path = cache / "RelLociAssembly.json"
    fetch(SOURCES["strbase_assembly"]["url"], strbase_path, None)

    hipstr: dict[str, Core] = {}
    for line in paths["hipstr_hg38"].read_text().splitlines():
        chrom, start, end, _period, copies, name = line.split("\t")[:6]
        if chrom == "chrY":
            hipstr[name] = Core(
                int(start), int(end), "HipSTR-references", float(copies)
            )

    strbase: dict[str, Core] = {}
    for row in json.loads(strbase_path.read_text()):
        assembly = row.get("assembly") or {}
        locus = row.get("loci") or {}
        if (
            assembly.get("buildingNum") == 38
            and locus.get("chromosomeId") == 24
            and row.get("locusStart")
        ):
            sub = row.get("subLocusName")
            name = locus["machineName"] + (f"_{sub}" if sub else "")
            start, end = sorted((row["locusStart"], row["locusEnd"]))
            strbase[name] = Core(start, end, "NIST STRBase", row.get("refCE"))

    ylt: dict[str, Core] = {}
    for line in paths["ylt_panel"].read_text().splitlines()[1:]:
        cols = next(iter([re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", line)]))
        if cols[1] != ".":
            ylt[cols[1]] = Core(
                int(cols[4]), int(cols[5]), "Y-LineageTracker", float(cols[7])
            )

    ystrkit: dict[str, tuple[int, int, str, str]] = {}
    for line in paths["ystrkit_info"].read_text(encoding="utf-8-sig").splitlines():
        cols = line.split("\t")
        if len(cols) >= 5 and not line.startswith(("#", "TITLE")):
            ystrkit[cols[0]] = (int(cols[1]), int(cols[2]), cols[3], cols[4])

    nist: dict[str, tuple[str, list[int]]] = {}
    page = paths["nist_ystr_fact"].read_text(encoding="latin-1")
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S | re.I):
        cells = [
            re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)
        ]
        if len(cells) == 5 and re.match(r"AC\d+|X\d+", cells[3]):
            values = [int(v) for v in re.findall(r"\d+", cells[4])]
            nist[cells[0]] = (cells[3].split()[0], values)
    return Sources(hipstr, strbase, ylt, ystrkit, nist)


def core_for(name: str, sources: Sources, ref19: str, ref38: str) -> Core:
    if name.startswith("ylt:"):
        return sources.ylt[name[4:]]
    if name.startswith("ystrkit:"):
        start, end, _motif, pattern = sources.ystrkit[name[8:]]
        window = ref19[start - 1 : end]
        best = max(
            re.finditer(pattern, window), key=lambda m: len(m.group(0)), default=None
        )
        if best is None:
            raise PanelError(f"no {pattern} in its GRCh37 window")
        s19 = start - 1 + best.start()
        e19 = start - 1 + best.end()
        found = locate(ref19, s19, e19, ref38)
        if not found:
            raise PanelError("GRCh37 window not found in GRCh38")
        return Core(found[0] + 1, found[1], "Y-STR_Kit (GRCh37 window)", None)
    sb = sources.strbase.get(STRBASE_NAMES.get(name, name))
    if sb:
        return sb
    if name in sources.hipstr:
        return sources.hipstr[name]
    return sources.ylt[name]


# Y-STR_Kit's names where they differ from the panel's.
YSTRKIT_NAMES = {"DYS389I": "DYS389i", "DYS389II": "DYS389ii"}


def ystrkit_count(name: str, sources: Sources, ref19: str) -> int | None:
    """Y-STR_Kit's count on GRCh37: motif matches in its filter regex's matches."""
    entry = sources.ystrkit.get(YSTRKIT_NAMES.get(name, name))
    if entry is None:
        return None
    start, end, motif, pattern = entry
    window = ref19[start - 1 : end]
    matched = "".join(m.group(0) for m in re.finditer(pattern, window))
    return len(re.findall(motif, matched)) or None


def value_votes(
    name: str,
    period: int,
    tracts: list[tuple[int, Core, int, int]],
    sources: Sources,
    ref19: str,
    ref38: str,
) -> dict[str, int]:
    """Other sources' GRCh38 counts for a single-copy marker."""
    votes: dict[str, int] = {}
    kit = ystrkit_count(name, sources, ref19)
    delta19 = 0
    for _p, _c, s, e in tracts:
        found = locate(ref38, s, e, ref19)
        if found is None:
            kit = None
            break
        delta19 += (e - s) - (found[1] - found[0])
    if kit is not None and delta19 % period == 0:
        votes["Y-STR_Kit"] = kit + delta19 // period
    ylt = sources.ylt.get(name)
    if ylt is not None and ylt.copies is not None:
        votes["Y-LineageTracker"] = int(ylt.copies)
    hipstr = [c.copies for _p, c, _s, _e in tracts if c.source == "HipSTR-references"]
    if hipstr and len(hipstr) == len(tracts):
        votes["HipSTR-references"] = sum(int(c) for c in hipstr if c is not None)
    return votes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hg38", required=True)
    parser.add_argument("--hg19", required=True)
    parser.add_argument("--t2t", required=True)
    parser.add_argument("--cache", default="tmp/ystr-sources")
    parser.add_argument("--out", default="src/wgsextract_cli/ydna/ystr")
    args = parser.parse_args()
    cache = Path(args.cache)
    sources = load_sources(cache)
    refs = {
        build: chrom_seq(fasta, y_contig(fasta))
        for build, fasta in (
            ("hg38", args.hg38),
            ("hg19", args.hg19),
            ("t2t", args.t2t),
        )
    }
    ref38 = refs["hg38"]

    bacs: dict[str, dict[str, str]] = {}
    markers, loci = [], []
    emitted: set[str] = set()
    calibration: dict[str, dict[str, object]] = {}
    for spec in parse_spec():
        name, period = spec["marker"], int(spec["period"])
        core_names = [c for c in spec["cores"].split(";") if c]
        group = name[:-1] if spec["kind"] == "multicopy" else name
        tracts: list[tuple[int, Core, int, int]] = []
        for core_name in core_names:
            try:
                core = core_for(core_name, sources, refs["hg19"], ref38)
                s, e = widen(ref38, core, period)
            except PanelError as err:
                print(f"{name}: {err}; not called", file=sys.stderr)
                spec["kind"] = "none"
                spec["note"] = f"Not called: {err}."
                tracts = []
                break
            # Overlapping parts are measured as one tract.
            if spec["kind"] == "sum" and tracts and s < tracts[-1][3]:
                part, first, prev_s, _prev_e = tracts.pop()
                tracts.append((part, first, prev_s, e))
            else:
                tracts.append((len(tracts) + 1, core, s, e))
        ref_value: float | None = None
        ref_source = ""
        nist = sources.nist.get(NIST_NAMES.get(name, name))
        if tracts and spec["kind"] != "multicopy" and nist:
            accession, values = nist
            value = values[-1] if name == "DYS389II" else values[0]
            bac_path = cache / f"{accession}.fa"
            if not bac_path.exists():
                url = (
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                    f"?db=nuccore&id={accession}&rettype=fasta&retmode=text"
                )
                fetch(url, bac_path, None)
            text = bac_path.read_text()
            header, *seq_lines = text.splitlines()
            bac = "".join(seq_lines).upper()
            bacs[accession] = {
                "version": header[1:].split()[0],
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            delta_bp: int | None = 0
            for _part, _core, s, e in tracts:
                bac_len = locate_in_bac(ref38, s, e, bac)
                if bac_len is None:
                    delta_bp = None
                    break
                delta_bp = (delta_bp or 0) + (e - s) - bac_len
            if delta_bp is not None and delta_bp % period == 0:
                ref_value = value + delta_bp // period
                ref_source = f"NIST reference allele ({bacs[accession]['version']})"
            elif delta_bp is not None:
                print(
                    f"{name}: BAC differs from GRCh38 by {delta_bp} bp", file=sys.stderr
                )
        votes: dict[str, int] = {}
        if tracts and spec["kind"] in ("single", "sum"):
            votes = value_votes(name, period, tracts, sources, refs["hg19"], ref38)
        if ref_value is None and votes:
            # No NIST allele: the value most sources agree on, else Y-STR_Kit's.
            tally: dict[int, list[str]] = {}
            for source, value in votes.items():
                tally.setdefault(value, []).append(source)
            best = max(tally, key=lambda v: (len(tally[v]), "Y-STR_Kit" in tally[v]))
            ref_value = best
            ref_source = "unverified: " + ", ".join(sorted(tally[best]))
        elif ref_value is None and tracts and spec["kind"] in ("single", "sum"):
            ref_value = sum((c.end - c.start + 1) // period for _p, c, _s, _e in tracts)
            ref_source = "unverified: repeat units in core"
        calibration[name] = {"chosen": ref_value, "source": ref_source, "votes": votes}
        if name in CALIBRATED:
            ref_value, ref_source = CALIBRATED[name]
        if ref_value is not None:
            ref_value += int(spec["ftdna_offset"])
        if spec["kind"] == "multicopy" and tracts:
            ref_source = "repeat units in each copy's core"

        for part, core, s, e in tracts if group not in emitted else ():
            ref_len = e - s
            for build, ref in refs.items():
                if build == "hg38":
                    bs, be = s, e
                else:
                    found = locate(ref38, s, e, ref)
                    if not found:
                        print(
                            f"{name} part {part}: not found in {build}", file=sys.stderr
                        )
                        continue
                    bs, be = found
                left, right = ref[bs - KMER : bs], ref[be : be + KMER]
                # A copy's GRCh38 count; reads are measured against hg38_length.
                copy_value = ""
                if spec["kind"] == "multicopy" and core.copies is not None:
                    copy_value = str(int(core.copies))
                loci.append(
                    {
                        "group": group,
                        "part": part,
                        "build": build,
                        "start": bs + 1,
                        "end": be,
                        "left": left,
                        "right": right,
                        "ref_length": be - bs,
                        "hg38_length": ref_len,
                        "copy_value": copy_value,
                        "core_source": core.source,
                    }
                )
        emitted.add(group)
        markers.append(
            {
                "marker": name,
                "group": group,
                "ftdna_order": spec["order"],
                "ftdna_panel": spec["panel"],
                "kits": spec["kits"],
                "period": period,
                "kind": spec["kind"],
                "hg38_value": "" if ref_value is None else ref_value,
                "value_source": ref_source,
                "note": spec["note"].strip(),
                # Last, as it is never empty: a trailing tab would be stripped.
                "ftdna_minus_nist": spec["ftdna_offset"],
            }
        )

    out = Path(args.out)
    marker_cols = list(markers[0])
    locus_cols = list(loci[0])
    with open(out / "markers.tsv", "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(marker_cols) + "\n")
        for m in markers:
            f.write("\t".join(str(m[c]) for c in marker_cols) + "\n")
    with open(out / "loci.tsv", "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(locus_cols) + "\n")
        for row in loci:
            f.write("\t".join(str(row[c]) for c in locus_cols) + "\n")
    provenance = {
        "generator": "scripts/build_ystr_panel.py",
        "kmer": KMER,
        "sources": {
            key: {k: v for k, v in src.items() if v}
            | {
                "retrieved_sha256": hashlib.sha256(
                    (
                        cache
                        / (
                            Path(src["url"]).name
                            if key != "strbase_assembly"
                            else "RelLociAssembly.json"
                        )
                    ).read_bytes()
                ).hexdigest()
            }
            for key, src in SOURCES.items()
        },
        "genbank_bacs": dict(sorted(bacs.items())),
        "reference_value_votes": calibration,
        "references": {
            build: {"fasta": Path(path).name, "contig": y_contig(path)}
            for build, path in (
                ("hg38", args.hg38),
                ("hg19", args.hg19),
                ("t2t", args.t2t),
            )
        },
    }
    (out / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"{len(markers)} markers, {len(loci)} loci", file=sys.stderr)


if __name__ == "__main__":
    main()
