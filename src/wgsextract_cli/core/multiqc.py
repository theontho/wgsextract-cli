"""MultiQC custom content from result manifests (#354).

Turns ``wgsextract.result/1`` documents (``*.manifest.json`` files and saved
``--json`` output) into MultiQC custom-content files, ``wgsextract_*_mqc.json``.
MultiQC's built-in custom_content module reads them; producing them needs no
MultiQC. Only the metrics commands already record on their manifests are used,
so nothing here re-parses logs or tool output.

Each sample gets one row per section. Its name is the primary input's file
name without its type extensions (``HG002.cram`` -> ``HG002``). When a sample
has several runs of one command, the most recent run's values win.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.results import MANIFEST_SUFFIX, SCHEMA_ID

FILE_PREFIX = "wgsextract_"
FILE_SUFFIX = "_mqc.json"
PARENT_ID = "wgsextract"
PARENT_NAME = "WGS Extract"
PARENT_DESCRIPTION = (
    "Metrics from WGS Extract result manifests "
    "(<code>wgsextract.result/1</code>); see docs/interop.md."
)
# A saved --json document starts with its schema; one larger than this is not ours.
_MAX_DOCUMENT_BYTES = 64 << 20
_SCHEMA_PROBE = re.compile(rb'"schema"\s*:\s*"wgsextract\.result/1"')
# Inputs that name the sample, most specific first.
_SAMPLE_ROLES = ("input", "vcf_input", "fastq_r1")
_GVCF_SUFFIXES = (".g", ".gvcf")
# These commands' manifests describe reports, not samples.
_OWN_COMMANDS = {"qc multiqc", "qc multiqc-metrics"}

Scalar = str | int | float | bool | None


@dataclass(frozen=True)
class Column:
    """A table column: a metric, how MultiQC shows it, and whether it also
    goes in the General Statistics table."""

    id: str
    title: str
    description: str
    format: str | None = None
    suffix: str | None = None
    scale: str | None = None
    min: float | None = None
    max: float | None = None
    general: bool = False
    hidden: bool = False

    def header(self) -> dict[str, Any]:
        header: dict[str, Any] = {"title": self.title, "description": self.description}
        for key in ("format", "suffix", "scale", "min", "max"):
            if (value := getattr(self, key)) is not None:
                header[key] = value
        if self.hidden:
            header["hidden"] = True
        return header


@dataclass
class Section:
    id: str
    name: str
    description: str
    columns: list[Column] = field(default_factory=list)
    rows: dict[str, dict[str, Scalar]] = field(default_factory=dict)

    def add(self, sample: str, values: Mapping[str, Scalar]) -> None:
        row = self.rows.setdefault(sample, {})
        row.update({k: v for k, v in values.items() if _present(v)})

    def used_columns(self) -> list[Column]:
        used = {key for row in self.rows.values() for key in row}
        return [column for column in self.columns if column.id in used]


def _present(value: Scalar) -> bool:
    return value is not None and not (isinstance(value, float) and math.isnan(value))


ALIGNMENT_COLUMNS = [
    Column("sex", "Sex", "Sex inferred from chrX/chrY coverage", general=True),
    Column(
        "ard_mapped",
        "Depth",
        "Average read depth of mapped reads over the genome",
        format="{:,.1f}",
        suffix="X",
        scale="RdYlGn",
        min=0.0,
        general=True,
    ),
    Column(
        "ard_raw",
        "Raw depth",
        "Average read depth of all reads",
        format="{:,.1f}",
        suffix="X",
        hidden=True,
    ),
    Column(
        "reads_mapped_m",
        "M mapped",
        "Mapped reads (millions)",
        format="{:,.1f}",
        scale="Blues",
    ),
    Column(
        "reads_mapped_pct",
        "% Mapped",
        "Mapped reads as a percentage of all reads",
        format="{:,.1f}",
        scale="RdYlGn",
        general=True,
        suffix="%",
        min=0.0,
        max=100.0,
    ),
    Column(
        "gbases_mapped",
        "Gb mapped",
        "Mapped bases (gigabases)",
        format="{:,.2f}",
        hidden=True,
    ),
    Column("avg_read_len", "Read length", "Average read length", format="{:,.0f}"),
    Column("read_n50", "Read N50", "Long reads' N50 in bp", format="{:,.0f}"),
    Column(
        "mapped_read_pct",
        "% Reads mapped",
        "Long reads mapped, counting each read once rather than each record",
        format="{:,.1f}",
        scale="RdYlGn",
        suffix="%",
        min=0.0,
        max=100.0,
    ),
    Column(
        "avg_insert_size",
        "Insert size",
        "Average insert size of paired reads",
        format="{:,.0f}",
    ),
    Column("is_paired", "Paired", "Whether the reads are paired-end"),
    Column(
        "duplicate_pct",
        "% Dups",
        "Reads flagged as duplicates",
        format="{:,.1f}",
        scale="OrRd",
        general=True,
        suffix="%",
        min=0.0,
        max=100.0,
    ),
    Column("duplicates_marked", "Dups marked", "Whether duplicates are marked"),
    Column(
        "alt_aware",
        "ALT-aware",
        "Whether ALT contigs were mapped ALT-aware (null without ALT contigs)",
    ),
    Column(
        "reference_model",
        "Reference",
        "Reference build inferred from the header (build, mito model, contigs)",
    ),
    Column("sequencer", "Sequencer", "Sequencer inferred from the read names"),
]

LINEAGE_COLUMNS = [
    Column("mt_haplogroup", "mtDNA haplogroup", "Haplogrep prediction", general=True),
    Column(
        "mt_quality",
        "mtDNA quality",
        "Haplogrep quality (0-1)",
        format="{:,.2f}",
        min=0.0,
        max=1.0,
        scale="RdYlGn",
    ),
    Column("y_haplogroup", "Y haplogroup", "Yleaf prediction", general=True),
    Column("y_marker", "Y marker", "Yleaf's terminal marker"),
    Column(
        "y_qc_score",
        "Y QC score",
        "Yleaf QC score (0-1)",
        format="{:,.2f}",
        min=0.0,
        max=1.0,
        scale="RdYlGn",
    ),
    Column("ystr_panel", "Y-STR panel", "Y-STR panel genotyped"),
    Column("ystr_called", "Y-STRs called", "Y-STR markers called", format="{:,.0f}"),
    Column(
        "ystr_markers",
        "Y-STR markers",
        "Y-STR markers in the panel",
        format="{:,.0f}",
        hidden=True,
    ),
]

MICROARRAY_COLUMNS = [
    Column(
        "autosomal_called",
        "Autosomal calls",
        "Autosomal markers called in the CombinedKit",
        format="{:,.0f}",
        scale="Blues",
    ),
    Column(
        "autosomal_homozygosity",
        "% Homozygous",
        "Homozygous share of the called autosomal markers",
        format="{:,.1f}",
        scale="PuOr",
        suffix="%",
        min=0.0,
        max=100.0,
    ),
    Column("kits", "Kits", "Vendor-format kits written", format="{:,.0f}"),
    Column(
        "max_no_call_pct",
        "% No-call (worst kit)",
        "Highest no-call rate among the vendor kits",
        format="{:,.2f}",
        scale="OrRd",
        suffix="%",
        min=0.0,
        max=100.0,
    ),
]

# Variant-count columns of the commands that report them; others get a generic one.
VARIANT_TITLES = {
    "vcf snp": ("SNPs", "SNVs called by bcftools (vcf snp)"),
    "vcf indel": ("InDels", "InDels called by bcftools (vcf indel)"),
    "vcf freebayes": ("FreeBayes", "Variants called by FreeBayes"),
    "vcf gatk": ("GATK", "Variants called by GATK HaplotypeCaller"),
    "vcf annotate": ("Annotated", "Variants in the annotated VCF"),
    "extract mito-vcf": ("mtDNA", "mtDNA variants (extract mito-vcf)"),
    "extract ydna-vcf": ("Y-DNA", "Y-DNA variants (extract ydna-vcf)"),
}

WARNING_COLUMNS = [
    Column(
        "warnings",
        "Warnings",
        "WGS Extract warnings across all runs",
        format="{:,.0f}",
        scale="OrRd",
        min=0.0,
        general=True,
    ),
    Column(
        "codes", "Warning codes", "Distinct warning codes (docs/result-manifest.md)"
    ),
]


def sample_name(document: Mapping[str, Any]) -> str | None:
    """The sample a manifest describes: the VCF sample it selected (e.g. from a
    joint-called VCF), else its primary input's name without type extensions;
    None when it has neither."""
    metrics = document.get("metrics")
    if isinstance(metrics, Mapping) and isinstance(metrics.get("sample"), str):
        return str(metrics["sample"])
    inputs = [i for i in document.get("inputs") or [] if isinstance(i, Mapping)]
    for role in _SAMPLE_ROLES:
        for record in inputs:
            if record.get("role") == role and record.get("path"):
                return _clean_stem(str(record["path"]))
    return None


def _clean_stem(path: str) -> str:
    stem = input_stem(os.path.basename(path))
    for suffix in (".gz", ".fastq", ".fq", *_GVCF_SUFFIXES):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem or os.path.basename(path)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _percent(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else round(number * 100, 4)


def _alignment(document: Mapping[str, Any]) -> dict[str, Scalar]:
    metrics = document.get("metrics") or {}
    return {column.id: metrics.get(column.id) for column in ALIGNMENT_COLUMNS}


def _lineage(document: Mapping[str, Any]) -> dict[str, Scalar]:
    metrics = document.get("metrics") or {}
    command = document.get("command")
    if command == "lineage mt-haplogroup":
        return {
            "mt_haplogroup": metrics.get("haplogroup"),
            "mt_quality": _number(metrics.get("haplogroup_quality")),
        }
    if command == "lineage y-haplogroup":
        return {
            "y_haplogroup": metrics.get("haplogroup"),
            "y_marker": metrics.get("haplogroup_marker"),
            "y_qc_score": _number(metrics.get("qc_score")),
        }
    return {
        "ystr_panel": metrics.get("panel"),
        "ystr_called": metrics.get("called"),
        "ystr_markers": metrics.get("markers"),
    }


def _microarray(document: Mapping[str, Any]) -> dict[str, Scalar]:
    metrics = document.get("metrics") or {}
    kits = [
        o
        for o in document.get("outputs") or []
        if isinstance(o, Mapping) and o.get("role") == "vendor_kit"
    ]
    rates = [
        rate
        for kit in kits
        if (rate := _number((kit.get("metrics") or {}).get("no_call_rate"))) is not None
    ]
    return {
        "autosomal_called": metrics.get("autosomal_called"),
        "autosomal_homozygosity": _percent(metrics.get("autosomal_homozygosity")),
        "kits": len(kits) or None,
        "max_no_call_pct": round(max(rates) * 100, 4) if rates else None,
    }


def _is_alignment(command: str) -> bool:
    return command == "info" or (command.startswith("info ") and command != "info chip")


_Extractor = Callable[[Mapping[str, Any]], dict[str, Scalar]]


def _extractor(command: str) -> tuple[str, _Extractor] | None:
    if _is_alignment(command):
        return "alignment", _alignment
    if command in {"lineage mt-haplogroup", "lineage y-haplogroup", "lineage y-str"}:
        return "lineage", _lineage
    if command == "microarray":
        return "microarray", _microarray
    return None


def _variant_column_id(command: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")


def build_sections(documents: Iterable[Mapping[str, Any]]) -> list[Section]:
    """MultiQC sections, with rows per sample, from successful manifests."""
    sections = {
        "alignment": Section(
            "alignment",
            "Alignment summary",
            "Read, depth and duplicate metrics, sex and reference build "
            "from <code>wgsextract info</code>.",
            ALIGNMENT_COLUMNS,
        ),
        "lineage": Section(
            "lineage",
            "Haplogroups",
            "mtDNA (Haplogrep) and Y (Yleaf) haplogroups and Y-STR panels "
            "from <code>wgsextract lineage</code>.",
            LINEAGE_COLUMNS,
        ),
        "microarray": Section(
            "microarray",
            "Microarray kits",
            "Genotype summary of the simulated consumer microarray kits "
            "from <code>wgsextract microarray</code>.",
            MICROARRAY_COLUMNS,
        ),
        "variants": Section(
            "variants",
            "Variant counts",
            "Records in the VCFs that WGS Extract called or extracted.",
        ),
        "warnings": Section(
            "warnings",
            "Warnings",
            "Structured warnings (low coverage, unknown sex, build problems ...) "
            "the runs raised.",
            WARNING_COLUMNS,
        ),
    }
    variant_columns: dict[str, Column] = {}
    codes: dict[str, set[str]] = {}
    counts: dict[str, int] = {}

    ordered = sorted(
        (d for d in documents if d.get("status") == "ok"),
        key=lambda d: str((d.get("provenance") or {}).get("finished") or ""),
    )
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for document in ordered:
        sample = sample_name(document)
        command = str(document.get("command") or "")
        if not sample or command in _OWN_COMMANDS:
            continue
        # Warnings accumulate over every run; metrics come from each command's
        # latest run alone, so a metric it no longer reports doesn't linger.
        warnings = [
            str(w["code"])
            for w in document.get("warnings") or []
            if isinstance(w, Mapping) and w.get("code")
        ]
        counts[sample] = counts.get(sample, 0) + len(warnings)
        codes.setdefault(sample, set()).update(warnings)
        latest[sample, command] = document
    for (sample, command), document in latest.items():
        handled = _extractor(command)
        if handled:
            section_id, extract = handled
            sections[section_id].add(sample, extract(document))
        variants = (document.get("metrics") or {}).get("variants")
        if _number(variants) is not None:
            column_id = _variant_column_id(command)
            title, description = VARIANT_TITLES.get(
                command, (command, f"Variants reported by {command}")
            )
            variant_columns.setdefault(
                column_id,
                Column(column_id, title, description, format="{:,.0f}", scale="Blues"),
            )
            sections["variants"].add(sample, {column_id: variants})
    sections["variants"].columns = sorted(variant_columns.values(), key=lambda c: c.id)
    for sample, count in counts.items():
        if count:
            sections["warnings"].add(
                sample, {"warnings": count, "codes": ", ".join(sorted(codes[sample]))}
            )
    return [section for section in sections.values() if section.rows]


def _table(section: Section) -> dict[str, Any]:
    section_id = f"{PARENT_ID}_{section.id}"
    columns = section.used_columns()
    return {
        "id": section_id,
        "parent_id": PARENT_ID,
        "parent_name": PARENT_NAME,
        "parent_description": PARENT_DESCRIPTION,
        "section_name": section.name,
        "description": section.description,
        "plot_type": "table",
        "pconfig": {
            "id": f"{section_id}_table",
            "title": f"{PARENT_NAME}: {section.name}",
            "namespace": PARENT_NAME,
        },
        "headers": {column.id: column.header() for column in columns},
        "data": {
            sample: {c.id: row[c.id] for c in columns if c.id in row}
            for sample, row in sorted(section.rows.items())
        },
    }


def _general_stats(sections: list[Section]) -> dict[str, Any] | None:
    headers: dict[str, Any] = {}
    data: dict[str, dict[str, Scalar]] = {}
    for section in sections:
        for column in section.used_columns():
            if not column.general:
                continue
            key = f"{PARENT_ID}_{column.id}"
            headers[key] = {**column.header(), "namespace": PARENT_NAME}
            for sample, row in section.rows.items():
                if column.id in row:
                    data.setdefault(sample, {})[key] = row[column.id]
    if not data:
        return None
    return {
        "id": f"{PARENT_ID}_general_stats",
        "plot_type": "generalstats",
        "namespace": PARENT_NAME,
        "headers": headers,
        "data": dict(sorted(data.items())),
    }


def custom_content(documents: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """File name -> MultiQC custom-content document, one per non-empty section
    plus General Statistics columns."""
    sections = build_sections(documents)
    files = {
        f"{FILE_PREFIX}{section.id}{FILE_SUFFIX}": _table(section)
        for section in sections
    }
    general = _general_stats(sections)
    if general:
        files[f"{FILE_PREFIX}general_stats{FILE_SUFFIX}"] = general
    return files


def is_result_document(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schema") == SCHEMA_ID


def _looks_like_result(path: Path) -> bool:
    if path.name.endswith(MANIFEST_SUFFIX):
        return True
    try:
        if path.stat().st_size > _MAX_DOCUMENT_BYTES:
            return False
        with open(path, "rb") as stream:
            return bool(_SCHEMA_PROBE.search(stream.read(4096)))
    except OSError:
        return False


def find_documents(
    source: str | os.PathLike[str],
) -> list[tuple[Path, dict[str, Any]]]:
    """Result documents in ``source`` with their paths: a manifest or saved
    ``--json`` file, or a directory searched recursively (hidden directories
    skipped)."""
    root = Path(source)
    if root.is_file():
        candidates = [root]
    else:
        candidates = []
        for directory, subdirs, files in os.walk(root):
            subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
            candidates.extend(
                Path(directory, name)
                for name in sorted(files)
                if name.endswith(".json") and not name.endswith(FILE_SUFFIX)
            )
    documents = []
    for path in candidates:
        if not _looks_like_result(path):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            logging.warning(f"Skipping unreadable result document {path}: {e}")
            continue
        if is_result_document(document):
            documents.append((path, document))
        else:
            logging.debug(f"Not a {SCHEMA_ID} document: {path}")
    return documents


def write_custom_content(
    files: Mapping[str, Mapping[str, Any]], outdir: str | os.PathLike[str]
) -> list[Path]:
    """Write ``custom_content()`` files into ``outdir``, removing ones an
    earlier run wrote for sections that now have no data."""
    from wgsextract_cli.core.atomic_output import atomic_output

    directory = Path(outdir)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in files.items():
        path = directory / name
        with atomic_output(path, retire_indexes=False) as staged:
            Path(staged).write_text(
                json.dumps(content, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        written.append(path)
    for stale in directory.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"):
        if stale.name not in files:
            stale.unlink()
    return written
