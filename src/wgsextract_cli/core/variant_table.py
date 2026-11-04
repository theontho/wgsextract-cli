"""One flat row per variant allele, from a VCF and whatever annotations it carries.

`vcf table` turns a VCF into a table an agent, a spreadsheet or SQL can query
without VCF knowledge: one row per ALT allele, fixed typed columns, no nested
values. The annotations come from the INFO fields our own commands write:

- VEP's ``CSQ`` (`vep run`) or SnpEff's ``ANN``: gene, consequence, impact
  and HGVS of one transcript per allele (see ``pick_csq``), and rsIDs,
  ClinVar and gnomAD values when the VCF lacks the dedicated fields below.
- ``CLNSIG``/``CLNDN`` (`vcf clinvar`), ``GNOMAD_AF`` (`vcf gnomad`),
  ``PHARMGKB`` (`vcf pharmgkb`) and ``WGSE_CONF``/``WGSE_CONF_REASONS`` (depth-aware
  confidence labels, `vcf clinvar`/`vcf gnomad`/`vcf pharmcat`).

An annotation the VCF lacks leaves its column empty. Records are parsed one at
a time, so a whole genome streams through in constant memory.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import unquote

from wgsextract_cli.core.variant_confidence import SPLIT_TAG

TEXT, INTEGER, REAL = "TEXT", "INTEGER", "REAL"


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # SQLite storage class: TEXT, INTEGER or REAL
    description: str


# The table's schema, in column order. Add a column by appending it here and
# filling it in ``variant_rows``; the writers and docs read this list.
COLUMNS: tuple[Column, ...] = (
    Column("chrom", TEXT, "Chromosome, as named in the VCF (chr1 or 1)."),
    Column("pos", INTEGER, "1-based position of REF."),
    Column("ref", TEXT, "Reference allele."),
    Column("alt", TEXT, "This row's alternate allele (one row per ALT)."),
    Column(
        "rsid",
        TEXT,
        "dbSNP rsID(s), ';'-separated: the VCF ID, else VEP's or PharmGKB's.",
    ),
    Column("gene", TEXT, "Gene symbol (VEP SYMBOL, SnpEff Gene_Name or PharmGKB)."),
    Column(
        "consequence",
        TEXT,
        "Sequence Ontology consequence(s) on the chosen transcript, '&'-separated "
        "(e.g. missense_variant).",
    ),
    Column("impact", TEXT, "VEP/SnpEff impact: HIGH, MODERATE, LOW or MODIFIER."),
    Column("hgvs_c", TEXT, "HGVS coding change on the chosen transcript."),
    Column("hgvs_p", TEXT, "HGVS protein change on the chosen transcript."),
    Column(
        "clinvar_significance",
        TEXT,
        "ClinVar clinical significance (CLNSIG, e.g. Pathogenic, "
        "Likely_pathogenic, Benign).",
    ),
    Column("clinvar_condition", TEXT, "ClinVar condition name(s) (CLNDN)."),
    Column("gnomad_af", REAL, "gnomAD allele frequency of this ALT (0-1)."),
    Column("genotype", TEXT, "The sample's GT, as in the VCF (e.g. 0/1, 1|1)."),
    Column(
        "zygosity",
        TEXT,
        "The sample's copies of this ALT: het, hom_alt, hemizygous, "
        "not_carried or no_call.",
    ),
    Column(
        "depth", INTEGER, "Read depth: FORMAT/DP, else the sum of AD, else INFO/DP."
    ),
    Column("gq", INTEGER, "Genotype quality (FORMAT/GQ)."),
    Column("qual", REAL, "Variant quality (QUAL)."),
    Column("filter", TEXT, "FILTER: PASS or the failed filters, ';'-separated."),
    Column(
        "confidence",
        TEXT,
        "Depth-aware confidence label: high, moderate or low (INFO/WGSE_CONF).",
    ),
    Column(
        "confidence_reasons",
        TEXT,
        "Reason codes behind the confidence label, '|'-separated "
        "(INFO/WGSE_CONF_REASONS).",
    ),
    Column(
        "pharmgkb_level",
        TEXT,
        "Best PharmGKB clinical-annotation level of evidence (1A strongest, "
        "then 1B, 2A, 2B, 3, 4).",
    ),
    Column("pharmgkb_drugs", TEXT, "Drugs in PharmGKB's annotations, '; '-separated."),
    Column(
        "pharmcat_diplotype",
        TEXT,
        "PharmCAT's diplotype for this row's gene (with --pharmcat-json).",
    ),
    Column(
        "pharmcat_phenotype",
        TEXT,
        "PharmCAT's phenotype for this row's gene (with --pharmcat-json).",
    ),
)
COLUMN_NAMES = tuple(column.name for column in COLUMNS)
Row = tuple[str | int | float | None, ...]

# Zygosities of rows the sample carries.
CARRIED = frozenset({"het", "hom_alt", "hemizygous"})

# ALTs that are not variants: a gVCF's reference-block allele and the
# spanning-deletion placeholder (the deletion has its own row).
_NON_VARIANT_ALTS = frozenset({".", "*", "<NON_REF>", "<*>", "<X>"})

# Ensembl's consequence order, most severe first
# (https://www.ensembl.org/info/genome/variation/prediction/predicted_data.html).
_SEVERITY = {
    term: rank
    for rank, term in enumerate(
        """transcript_ablation splice_acceptor_variant splice_donor_variant
        stop_gained frameshift_variant stop_lost start_lost
        transcript_amplification feature_elongation feature_truncation
        inframe_insertion inframe_deletion missense_variant
        protein_altering_variant splice_donor_5th_base_variant
        splice_region_variant splice_donor_region_variant
        splice_polypyrimidine_tract_variant incomplete_terminal_codon_variant
        start_retained_variant stop_retained_variant synonymous_variant
        coding_sequence_variant mature_miRNA_variant 5_prime_UTR_variant
        3_prime_UTR_variant non_coding_transcript_exon_variant intron_variant
        NMD_transcript_variant non_coding_transcript_variant
        coding_transcript_variant upstream_gene_variant downstream_gene_variant
        TFBS_ablation TFBS_amplification TF_binding_site_variant
        regulatory_region_ablation regulatory_region_amplification
        regulatory_region_variant intergenic_variant sequence_variant""".split()
    )
}
_UNRANKED = len(_SEVERITY)

# VEP gnomAD frequency fields, preferred first: genomes (whole-genome
# sequencing, like ours), then exomes, then the pre-v4 combined field.
_VEP_GNOMAD_AF = ("gnomADg_AF", "gnomADe_AF", "gnomAD_AF")
_PHARMGKB_LEVELS = ("1A", "1B", "2A", "2B", "3", "4")

_INFO_LINE = re.compile(
    r'^##INFO=<ID=([^,>]+),Number=([^,>]+),.*?Description="(.*)"', re.DOTALL
)
_FORMAT_LIST = re.compile(r"Format:\s*(.+)$")
_GT_SPLIT = re.compile(r"[/|]")


@dataclass
class VcfHeader:
    """What the table needs from a VCF header."""

    info_number: dict[str, str] = field(default_factory=dict)
    csq_fields: list[str] | None = None
    ann_fields: list[str] | None = None
    samples: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def annotations(self) -> list[str]:
        """The annotation sources present, for provenance and the manifest."""
        found = []
        if self.csq_fields:
            found.append("vep")
        if self.ann_fields:
            found.append("snpeff")
        for name, key in (
            ("clinvar", "CLNSIG"),
            ("gnomad", "GNOMAD_AF"),
            ("pharmgkb", "PHARMGKB"),
            ("confidence", "WGSE_CONF"),
        ):
            if key in self.info_number:
                found.append(name)
        return found


def _subfield_names(description: str) -> list[str]:
    """VEP's 'Format: Allele|Consequence|...' or SnpEff's
    "Functional annotations: 'Allele | Annotation | ...'" as a list of names."""
    match = _FORMAT_LIST.search(description)
    names = match.group(1) if match else description.partition(":")[2]
    return [name.strip(" '\"") for name in names.split("|")]


def parse_header(lines: Iterable[str]) -> VcfHeader:
    """Parse ``##`` header lines and the ``#CHROM`` line."""
    header = VcfHeader()
    for line in lines:
        line = line.rstrip("\r\n")
        header.lines.append(line)
        if line.startswith("#CHROM"):
            header.samples = line.split("\t")[9:]
            continue
        match = _INFO_LINE.match(line)
        if not match:
            continue
        key, number, description = match.groups()
        header.info_number[key] = number
        if key == "CSQ":
            header.csq_fields = _subfield_names(description)
        elif key == "ANN":
            header.ann_fields = _subfield_names(description)
    return header


def _value(raw: str | None) -> str | None:
    return None if raw in (None, "", ".") else raw


def _to_int(raw: str | None) -> int | None:
    raw = _value(raw)
    if raw is None:
        return None
    try:
        return round(float(raw))
    except ValueError:
        return None


def _to_float(raw: str | None) -> float | None:
    raw = _value(raw)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _per_allele(raw: str | None, number: str | None, alt_index: int) -> str | None:
    """This ALT's value of an INFO field (``alt_index`` is 1-based)."""
    if raw is None:
        return None
    if number in ("A", "R"):
        values = raw.split(",")
        i = alt_index - 1 if number == "A" else alt_index
        return _value(values[i]) if i < len(values) else None
    return _value(raw)


# Ensembl VEP's test for a structural-variant ALT (<DEL>, breakends), which it
# names differently; <*> doesn't match it (Parser/VCF.pm, release 113).
_SV_ALT = re.compile(r"[<\[\]][^*]+[>\]\[]|^\.\w+|\w+\.$")
_BASES = re.compile(r"[ACGT]+")


def vep_allele(ref: str, alts: Sequence[str], alt: str) -> str:
    """How VEP names ``alt`` in CSQ, following its VCF parser: an indel record
    loses its shared padding base ('-' when nothing is left). A multi-allelic
    record is trimmed when every allele without a '*' (spanning deletions,
    ``<*>``) starts with the same base, and those '*' alleles keep theirs."""
    if not _BASES.fullmatch(ref + "".join(alts)) and _SV_ALT.search(",".join(alts)):
        return alt
    if all(len(a) == len(ref) for a in alts):
        return alt
    if len(alts) > 1:
        first_bases = {a[:1] for a in (ref, *alts) if "*" not in a}
        if len(first_bases) != 1 or "*" in alt:
            return alt
    elif ref[:1] != alt[:1]:
        return alt
    return alt[1:] or "-"


def _severity(consequence: str) -> int:
    return min(
        (_SEVERITY.get(term, _UNRANKED) for term in consequence.split("&")),
        default=_UNRANKED,
    )


def pick_csq(entries: Sequence[Mapping[str, str]]) -> Mapping[str, str] | None:
    """The CSQ entry that describes an allele, as VEP's ``--pick`` would choose it:
    one VEP flagged with PICK, else the MANE Select transcript, then the
    canonical one, then a protein-coding one, then the most severe consequence."""
    if not entries:
        return None
    flagged = [e for e in entries if e.get("PICK") == "1"]
    if flagged:
        return flagged[0]
    return min(
        entries,
        key=lambda e: (
            not e.get("MANE_SELECT"),
            e.get("CANONICAL") != "YES",
            e.get("BIOTYPE") != "protein_coding",
            _severity(e.get("Consequence", "")),
        ),
    )


def _entries(raw: str | None, names: Sequence[str]) -> list[dict[str, str]]:
    if not raw:
        return []
    return [
        dict(zip(names, entry.split("|"), strict=False)) for entry in raw.split(",")
    ]


def _for_allele(
    entries: list[dict[str, str]],
    allele_key: str,
    candidates: Sequence[str],
    alt_index: int,
    n_alts: int,
    *,
    split: bool = False,
) -> list[dict[str, str]]:
    """The entries describing one ALT. ``candidates`` are its possible
    spellings, most specific first: the first that matches anything wins, so
    VEP's trimmed spelling of one insertion can't also pick up another's.

    A record split from a multi-allelic one (``split``: ``vcf clinvar``/
    ``gnomad`` split them) may keep every original allele's entries, so it is
    matched by its original ALLELE_NUM (``alt_index``) or spelling, never
    given entries just because they all name one allele."""
    if not entries:
        return []
    if "ALLELE_NUM" in entries[0] and (
        n_alts > 1 or (split and {e.get("ALLELE_NUM") for e in entries} != {"1"})
    ):
        return [e for e in entries if e.get("ALLELE_NUM") == str(alt_index)]
    if n_alts == 1 and not split and len({e.get(allele_key) for e in entries}) == 1:
        return entries
    for spelling in dict.fromkeys(candidates):
        matches = [e for e in entries if e.get(allele_key) == spelling]
        if matches:
            return matches
    return []


def _split_origin(raw: str | None) -> tuple[str, list[str], int] | None:
    """REF, ALTs and this ALT's 1-based index of the record a biallelic one
    was split from (bcftools norm ``--old-rec-tag``: CHR|POS|REF|ALT|IDX)."""
    parts = (raw or "").split("|")
    if len(parts) != 5 or not parts[4].isdigit():
        return None
    alts = parts[3].split(",")
    index = int(parts[4])
    return (parts[2], alts, index) if 1 <= index <= len(alts) else None


def zygosity(gt: str | None, alt_index: int) -> str | None:
    """The sample's copies of ALT number ``alt_index`` (1-based) in ``gt``."""
    if gt is None:
        return None
    alleles = _GT_SPLIT.split(gt)
    called = [a for a in alleles if a != "."]
    if not called:
        return "no_call"
    copies = called.count(str(alt_index))
    if copies == 0:
        return "not_carried"
    if len(alleles) == 1:
        return "hemizygous"
    return "hom_alt" if copies == len(alleles) else "het"


def _rsids(
    vcf_id: str, csq: Mapping[str, str] | None, pharmgkb: str | None
) -> str | None:
    ids = [i for i in vcf_id.split(";") if i.startswith("rs")]
    if not ids and csq:
        ids = [
            i
            for i in csq.get("Existing_variation", "").split("&")
            if i.startswith("rs")
        ]
    if not ids and pharmgkb:
        ids = [e.split("|", 1)[0] for e in pharmgkb.split(",")]
        ids = [i for i in dict.fromkeys(ids) if i.startswith("rs")]
    return ";".join(ids) or None


def _pharmgkb(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """(gene, best level, drugs) from `vcf pharmgkb`'s rsID|gene|level|category|drugs."""
    if not raw:
        return None, None, None
    genes: list[str] = []
    levels: list[str] = []
    drugs: list[str] = []
    for entry in raw.split(","):
        parts = entry.split("|")
        if len(parts) < 5:
            continue
        genes.append(parts[1])
        levels.append(parts[2])
        drugs.extend(d.strip() for d in parts[4].split("/") if d.strip())
    ranked = [lvl for lvl in _PHARMGKB_LEVELS if lvl in levels]
    gene = next((g for g in genes if g), None)
    return gene, (ranked[0] if ranked else None), "; ".join(sorted(set(drugs))) or None


def _info(raw: str) -> dict[str, str]:
    if raw in ("", "."):
        return {}
    info = {}
    for item in raw.split(";"):
        key, _, value = item.partition("=")
        info[key] = value
    return info


def variant_rows(
    records: Iterable[str],
    header: VcfHeader,
    *,
    sample_index: int | None = 0,
    carried_only: bool = True,
    pharmcat: Mapping[str, tuple[str, str]] | None = None,
) -> Iterator[Row]:
    """One row (in ``COLUMNS`` order) per ALT allele of each VCF data line.

    ``sample_index`` picks the sample whose genotype fills the genotype
    columns (None for a sites-only VCF). With ``carried_only``, ALTs that
    sample doesn't carry (0/0 calls, the other ALT of a 0/1 multi-allelic
    site, no-calls) are left out.
    """
    numbers = header.info_number
    csq_names = header.csq_fields or []
    ann_names = header.ann_fields or []
    pharmcat = pharmcat or {}
    for line in records:
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) < 8:
            continue
        chrom, pos_text, vcf_id, ref, alt_text, qual_text, filt, info_text = fields[:8]
        alts = alt_text.split(",")
        if all(a in _NON_VARIANT_ALTS for a in alts):
            continue
        info = _info(info_text)
        gt = sample_depth = gq = None
        sample_column = None if sample_index is None else 9 + sample_index
        has_sample = sample_column is not None and len(fields) > sample_column
        if sample_column is not None and has_sample:
            sample = dict(
                zip(
                    fields[8].split(":"),
                    fields[sample_column].split(":"),
                    strict=False,
                )
            )
            gt = _value(sample.get("GT"))
            sample_depth = _to_int(sample.get("DP"))
            if sample_depth is None and _value(sample.get("AD")):
                ad = [_to_int(v) for v in sample["AD"].split(",")]
                if all(v is not None for v in ad):
                    sample_depth = sum(v for v in ad if v is not None)
            gq = _to_int(sample.get("GQ"))
        depth = sample_depth if sample_depth is not None else _to_int(info.get("DP"))
        pos = int(pos_text)
        qual = _to_float(qual_text)
        filter_value = _value(filt)
        csq_all = _entries(info.get("CSQ"), csq_names)
        ann_all = _entries(info.get("ANN"), ann_names)
        origin = _split_origin(info.get(SPLIT_TAG))

        for alt_index, alt in enumerate(alts, start=1):
            if alt in _NON_VARIANT_ALTS:
                continue
            zyg = None
            if has_sample:
                zyg = zygosity(gt, alt_index) or "no_call"
            if carried_only and zyg is not None and zyg not in CARRIED:
                continue
            csq_spellings: tuple[str, ...] = (vep_allele(ref, alts, alt), alt)
            ann_spellings: tuple[str, ...] = (alt,)
            index, n_alts = alt_index, len(alts)
            split = origin is not None and n_alts == 1
            if origin is not None and split:
                o_ref, o_alts, index = origin
                o_alt = o_alts[index - 1]
                o_vep = vep_allele(o_ref, o_alts, o_alt)
                csq_spellings = (o_vep, *csq_spellings, o_alt)
                ann_spellings = (alt, o_alt)
            csq = pick_csq(
                _for_allele(
                    csq_all, "Allele", csq_spellings, index, n_alts, split=split
                )
            )
            ann_matches = _for_allele(
                ann_all, "Allele", ann_spellings, index, n_alts, split=split
            )
            # SnpEff lists an allele's effects most severe first.
            ann = ann_matches[0] if ann_matches else None
            pgx = _per_allele(info.get("PHARMGKB"), numbers.get("PHARMGKB"), alt_index)
            pgx_gene, pgx_level, pgx_drugs = _pharmgkb(pgx)
            gene = (
                _value((csq or {}).get("SYMBOL"))
                or _value((ann or {}).get("Gene_Name"))
                or pgx_gene
            )
            gnomad = _per_allele(
                info.get("GNOMAD_AF"), numbers.get("GNOMAD_AF"), alt_index
            )
            if gnomad is None and csq:
                gnomad = next(
                    (v for k in _VEP_GNOMAD_AF if (v := _value(csq.get(k)))), None
                )
            clnsig = _per_allele(info.get("CLNSIG"), numbers.get("CLNSIG"), alt_index)
            if clnsig is None and csq:
                clnsig = _value(csq.get("CLIN_SIG"))
            hgvs_p = _value((csq or {}).get("HGVSp")) or _value(
                (ann or {}).get("HGVS.p")
            )
            diplotype, phenotype = pharmcat.get(gene or "", (None, None))
            yield (
                chrom,
                pos,
                ref,
                alt,
                _rsids(vcf_id, csq, pgx),
                gene,
                _value((csq or {}).get("Consequence"))
                or _value((ann or {}).get("Annotation")),
                _value((csq or {}).get("IMPACT"))
                or _value((ann or {}).get("Annotation_Impact")),
                _value((csq or {}).get("HGVSc")) or _value((ann or {}).get("HGVS.c")),
                unquote(hgvs_p) if hgvs_p else None,  # VEP writes '=' as %3D
                clnsig,
                _per_allele(info.get("CLNDN"), numbers.get("CLNDN"), alt_index),
                _to_float(gnomad),
                gt,
                zyg,
                depth,
                gq,
                qual,
                filter_value,
                _per_allele(info.get("WGSE_CONF"), numbers.get("WGSE_CONF"), alt_index),
                _per_allele(
                    info.get("WGSE_CONF_REASONS"),
                    numbers.get("WGSE_CONF_REASONS"),
                    alt_index,
                ),
                pgx_level,
                pgx_drugs,
                diplotype,
                phenotype,
            )
