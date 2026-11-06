"""``report``: a sample's results as one local HTML, Markdown and JSON report.

The report is assembled from what earlier commands left in the sample folder:
their result manifests (``*.manifest.json``, docs/result-manifest.md) and the
sample workspace (``wgsextract.sample.toml``, docs/sample-workspace.md). It
runs no analysis. Its only network use is downloading a built-in gene list
(core/gene_lists.py) into the reference library the first time ClinVar calls
need one. A section whose command has not run says so and gives the command
that produces it.

Every finding carries a confidence label (``high``, ``moderate``, ``low``, or
``not_assessed`` when nothing supports a label), its reasons, the evidence
file (relative to the sample folder) with the region, and the "technical
result, not diagnosis" disclaimer. The JSON document follows
``assets/schemas/wgsextract.report.1.schema.json``; docs/personal-report.md
describes it.
"""

from __future__ import annotations

import csv
import gzip
import html
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

from wgsextract_cli import __version__
from wgsextract_cli.core import gene_lists, read_scope, variant_confidence
from wgsextract_cli.core.builds import (
    build_from_chr1_length,
    canonical_build,
    is_hg37_build,
    is_hg38_build,
)
from wgsextract_cli.core.multiqc import find_documents
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.reference_models import sequence_lengths
from wgsextract_cli.core.sample_workspace import WORKSPACE_FILE, SampleWorkspace
from wgsextract_cli.core.table_writer import Column, Value
from wgsextract_cli.core.utils import UsageError

COMMAND = "report"
SCHEMA_ID = "wgsextract.report/1"
SCHEMA_RESOURCE = "assets/schemas/wgsextract.report.1.schema.json"
GUIDE = "docs/personal-report.md"
NOT_DIAGNOSIS = "Technical result, not diagnosis."
DISCLAIMER = (
    "This report summarises technical results of software run on this "
    "computer; it is not a diagnosis or medical advice. "
    + variant_confidence.DISCLAIMER
)
NOT_ASSESSED = "not_assessed"
CONFIDENCE = (*variant_confidence.LABELS, NOT_ASSESSED)
# Haplogroup callers' own 0-1 quality scores (Yleaf QC-score, Haplogrep quality).
HIGH_QUALITY, LOW_QUALITY = 0.9, 0.7
REASONS = {
    **variant_confidence.REASONS,
    "low_caller_quality": f"the caller's quality score is below {LOW_QUALITY}",
    "moderate_caller_quality": f"the caller's quality score is below {HIGH_QUALITY}",
    "quality_unknown": "the caller reported no quality score",
    "low_coverage": "the run warned that coverage is too low for reliable calls",
    "caller_filter": "the caller flagged the call (its filter did not pass)",
    "no_call": "the caller made no call",
    "evidence_mismatch": "the evidence file no longer matches the run's result",
    "no_labelled_calls": "the gene has no labelled variant calls; a reference "
    "(*1) allele rests on coverage alone",
}
SECTIONS = {
    "overview": "Sample overview",
    "lineage": "Lineage",
    "pharmacogenomics": "Pharmacogenomics",
    "clinvar": "ClinVar pathogenic / likely pathogenic in {gene_list} genes",
    "microarray": "Microarray export",
}
_ALIGNMENT_SUFFIXES = (".bam", ".cram")
_VCF_SUFFIXES = (".vcf.gz", ".vcf.bgz", ".vcf", ".bcf")
# Manifest input roles naming the sample's own files
SAMPLE_ROLES = ("input", "vcf_input")
_OVERVIEW_FACTS = (
    ("sex", "Sex"),
    ("platform", "Platform"),
    ("build", "Build"),
    ("mean_coverage", "Mean coverage"),
    ("target_depths", "Depth by target"),
    ("read_length", "Read length"),
)
_INFO_FACTS = (
    ("sex", "sex"),
    ("mean_coverage", "ard_mapped"),
    ("read_length", "avg_read_len"),
)
_QC_METRICS = (
    ("sequencer", "Sequencer", ""),
    ("ard_mapped", "Mean depth (mapped)", "x"),
    ("reads_mapped_pct", "Reads mapped", "%"),
    ("duplicate_pct", "Duplicates", "%"),
    ("avg_read_len", "Mean read length", " bp"),
    ("avg_insert_size", "Mean insert size", " bp"),
    ("size_gb", "File size", " GB"),
)
_MICROARRAY_ROLES = ("combined_kit", "vendor_kit", "aadr")

# The findings table (``--format``): one row per finding, for agents and
# spreadsheets. The JSON document holds the same findings with their links.
TEXT, INTEGER, REAL = "TEXT", "INTEGER", "REAL"
FINDING_COLUMNS: tuple[Column, ...] = (
    Column("section", TEXT, "Report section: lineage, pharmacogenomics or clinvar."),
    Column("label", TEXT, "What the finding is, e.g. 'BRCA2 Pathogenic'."),
    Column("value", TEXT, "The result: haplogroup, diplotype or ClinVar class."),
    Column("detail", TEXT, "Phenotype, condition or caller detail."),
    Column("gene", TEXT, "Gene symbol, when the finding is in a gene."),
    Column("chrom", TEXT, "Chromosome of a variant finding."),
    Column("pos", INTEGER, "1-based position of a variant finding."),
    Column("ref", TEXT, "Reference allele."),
    Column("alt", TEXT, "Alternate allele."),
    Column("genotype", TEXT, "The sample's GT."),
    Column("zygosity", TEXT, "het, hom_alt or hemizygous."),
    Column("depth", INTEGER, "Read depth at the call."),
    Column("genotype_quality", INTEGER, "Genotype quality (GQ)."),
    Column("alt_fraction", REAL, "Fraction of reads carrying the ALT."),
    Column(
        "confidence", TEXT, "Confidence label: high, moderate, low or not_assessed."
    ),
    Column("confidence_reasons", TEXT, "Reason codes, '|'-separated."),
    Column("evidence_file", TEXT, "Evidence file, relative to the sample folder."),
    Column("evidence_region", TEXT, "Region of the evidence (chrom:pos, gene)."),
    Column("disclaimer", TEXT, "Technical result, not diagnosis."),
)


# --- result manifests --------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """A result manifest the report reads, and where it was found."""

    manifest: Path
    document: Mapping[str, Any]

    @property
    def command(self) -> str:
        return str(self.document.get("command") or "")

    @property
    def status(self) -> str:
        return str(self.document.get("status") or "")

    @property
    def finished(self) -> str:
        provenance = self.document.get("provenance") or {}
        return str(provenance.get("finished") or provenance.get("started") or "")

    @property
    def metrics(self) -> Mapping[str, Any]:
        return self.document.get("metrics") or {}

    @property
    def details(self) -> Mapping[str, Any]:
        details = self.document.get("details")
        return details if isinstance(details, Mapping) else {}

    def records(self, kind: str) -> list[Mapping[str, Any]]:
        return [r for r in self.document.get(kind) or [] if isinstance(r, Mapping)]

    def output(self, *roles: str) -> Path | None:
        """The first existing output with one of ``roles``, in ``roles`` order,
        that this run may read (see :mod:`read_scope`)."""
        for role in roles:
            for record in self.records("outputs"):
                if record.get("role") == role and record.get("path"):
                    path = self.locate(str(record["path"]))
                    if path.exists() and read_scope.may_follow(path):
                        return path
        return None

    def files(self, kind: str) -> list[Path]:
        return [
            self.locate(str(r["path"])) for r in self.records(kind) if r.get("path")
        ]

    def locate(self, recorded: str) -> Path:
        """A path the manifest recorded; when the folder has been moved or
        copied since, the same file relative to the manifest's new place
        (if it is there), so a copy reports on its own files."""
        path = Path(recorded)
        original = self.document.get("manifest")
        if isinstance(original, str):
            try:
                relative = path.relative_to(Path(original).parent)
            except ValueError:
                return path
            moved = self.manifest.parent / relative
            if moved.exists():
                return moved
        return path

    @property
    def recorded_folder(self) -> str | None:
        """The folder the manifest was written in, as it recorded it."""
        original = self.document.get("manifest")
        return str(Path(original).parent) if isinstance(original, str) else None

    def sample_files(self) -> list[Path]:
        """The sample files the run read (not references or resources)."""
        return [
            self.locate(str(r["path"]))
            for r in self.records("inputs")
            if r.get("role") in SAMPLE_ROLES and r.get("path")
        ]

    def warnings(self) -> list[dict[str, str]]:
        return [
            {"code": str(w.get("code", "")), "message": str(w.get("message", ""))}
            for w in self.records("warnings")
        ]


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(os.path.abspath(folder))
    except ValueError:
        return False
    return True


def collect_sources(folder: Path, extra: Iterable[Path] = ()) -> list[Source]:
    """The result manifests in ``folder`` (searched recursively), plus those in
    the ``extra`` folders that belong to this sample: whose inputs are files in
    ``folder`` or outputs of a manifest already kept."""
    found: dict[str, Source] = {}
    for path, document in find_documents(folder):
        if document.get("command") != COMMAND:
            found[_key(path)] = Source(Path(os.path.abspath(path)), document)
    candidates: dict[str, Source] = {}
    for directory in extra:
        for path, document in find_documents(directory):
            key = _key(path)
            if document.get("command") != COMMAND and key not in found:
                candidates[key] = Source(Path(os.path.abspath(path)), document)
    produced = {_key(p) for source in found.values() for p in source.files("outputs")}
    while candidates:
        kept = [
            key
            for key, source in candidates.items()
            if any(
                _inside(p, folder) or _key(p) in produced
                for p in source.files("inputs")
            )
        ]
        if not kept:
            break
        for key in kept:
            source = found[key] = candidates.pop(key)
            produced.update(_key(p) for p in source.files("outputs"))
    return sorted(found.values(), key=lambda s: (s.finished, str(s.manifest)))


def _files(source: Source) -> set[str]:
    return {_key(p) for p in [*source.sample_files(), *source.files("outputs")]}


def sample_groups(sources: Sequence[Source]) -> list[list[Source]]:
    """``sources`` grouped by sample: runs are linked when one read a file
    another read or wrote."""
    groups: list[tuple[set[str], list[Source]]] = []
    for source in sources:
        files = _files(source)
        linked = [g for g in groups if g[0] & files]
        merged: tuple[set[str], list[Source]] = (files, [source])
        for group in linked:
            groups.remove(group)
            merged[0].update(group[0])
            merged[1].extend(group[1])
        groups.append(merged)
    return [members for _, members in groups]


def _roots(group: Sequence[Source]) -> list[Path]:
    """The group's sample files no run in it produced: what it started from."""
    produced = {_key(p) for s in group for p in s.files("outputs")}
    roots = {_key(p): p for s in group for p in s.sample_files()}
    return [p for key, p in roots.items() if key not in produced]


def select_sample(
    sources: Sequence[Source], input_file: Path | None, *, workspace: bool
) -> list[Source]:
    """The runs on this sample. Runs on ``input_file`` and on files made from
    it (or that made it); in a sample workspace, which holds one person, also
    runs on files made from no other alignment (a VCF from elsewhere). With
    no ``input_file`` outside a workspace, every run unless they started from
    different samples' files, which is an error."""
    groups = sample_groups(sources)
    if input_file is not None:
        key = _key(input_file)
        kept = [
            g
            for g in groups
            if any(key in _files(s) for s in g)
            or (
                workspace
                and not any(
                    p.name.lower().endswith(_ALIGNMENT_SUFFIXES) for p in _roots(g)
                )
            )
        ]
        return sorted(
            (s for g in kept for s in g), key=lambda s: (s.finished, str(s.manifest))
        )
    if not workspace:
        stems = sorted({input_stem(p) for g in groups for p in _roots(g)})
        if len(stems) > 1:
            raise UsageError(
                "The folder has results of several samples ("
                + ", ".join(stems)
                + "); choose one with --input."
            )
    return list(sources)


def sample_sources(
    folder: Path, input_file: Path | None = None, results: Iterable[Path] = ()
) -> list[Source]:
    """The runs of the sample in ``folder`` (see :func:`select_sample`)."""
    workspace = SampleWorkspace(folder)
    return select_sample(
        collect_sources(folder, results),
        input_file,
        workspace=workspace.path.exists() or workspace.is_genome_folder,
    )


def latest(sources: Sequence[Source], match: Callable[[Source], bool]) -> Source | None:
    """The newest successful matching run, else the newest failed one."""
    hits = [s for s in sources if match(s)]
    ok = [s for s in hits if s.status == "ok"]
    return max(ok or hits, key=lambda s: s.finished, default=None)


def _command(name: str) -> Callable[[Source], bool]:
    return lambda source: source.command == name


def _command_or_role(command: str, *roles: str) -> Callable[[Source], bool]:
    """Runs of ``command`` (failed ones may have no outputs), or any run that
    wrote one of ``roles``."""
    return lambda source: (
        source.command == command
        or any(record.get("role") in roles for record in source.records("outputs"))
    )


# --- the gene list's build ---------------------------------------------------


def _gene_build(build: str | None) -> str | None:
    if not build:
        return None
    if is_hg38_build(build):
        return "hg38"
    if is_hg37_build(build):
        return "hg19"
    return None


def _vcf_build(path: Path) -> str | None:
    """The build a VCF's ##contig lines name, read locally from its header."""
    lines = []
    try:
        opener = gzip.open if path.name.endswith(("gz", "bgz")) else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.startswith("##"):
                    break
                lines.append(line)
    except (OSError, EOFError):
        return None
    return canonical_build(build_from_chr1_length(sequence_lengths("".join(lines))))


# --- building the report -----------------------------------------------------


@dataclass
class _Context:
    folder: Path
    sources: list[Source]
    workspace: dict[str, Any]
    alignment: str | None
    vcf: str | None
    gene_list: str | Path | None = None

    def rel(self, path: Path | str | None) -> str | None:
        """``path`` relative to the sample folder ('/'-separated), or absolute
        when it is on another drive (Windows) and so has no relative path."""
        if path is None:
            return None
        absolute = os.path.abspath(path)
        try:
            relative = os.path.relpath(absolute, self.folder)
        except ValueError:
            return Path(absolute).as_posix()
        return Path(relative).as_posix()

    def fact(self, name: str) -> dict[str, Any] | None:
        effective = self.workspace.get("effective") or {}
        if name in effective:
            return dict(effective[name])
        shared = (self.workspace.get("overrides") or {}).get(name)
        if shared is not None:
            return {"name": name, "value": shared, "source": "override"}
        for entry in (self.workspace.get("files") or {}).values():
            if entry.get("status") == "current" and name in entry.get("facts", {}):
                return dict(entry["facts"][name])
        return None

    def build_of(self, file: Path | None) -> str | None:
        """``file``'s build as the workspace recorded it, while still current."""
        key = self.rel(file) if file else None
        entry = (self.workspace.get("files") or {}).get(key or "") or {}
        fact = entry.get("facts", {}).get("build")
        return str(fact["value"]) if fact and entry.get("status") == "current" else None

    def command_for(self, command: str, input_file: str | None, what: str) -> str:
        target = input_file or f"YOUR.{what}"
        flag = "--vcf-input" if command.startswith("vcf ") and what != "cram" else "-i"
        return f"wgsextract {command} {flag} {_quote(target)}"


def _quote(text: str) -> str:
    return f'"{text}"' if any(c.isspace() for c in text) else text


def _finding(
    section: str,
    label: str,
    value: str,
    *,
    confidence: str,
    reasons: Sequence[str],
    basis: str,
    evidence: str | None,
    region: str | None,
    manifest: str | None,
    detail: str | None = None,
    gene: str | None = None,
    note: str | None = None,
    variant: Mapping[str, Any] | None = None,
    links: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    return {
        "section": section,
        "label": label,
        "value": value,
        "detail": detail,
        "gene": gene,
        "note": note,
        "confidence": confidence,
        "confidence_reasons": list(reasons),
        "confidence_basis": basis,
        "evidence": {"file": evidence, "region": region, "manifest": manifest},
        "variant": dict(variant) if variant else None,
        "links": [{"label": label, "url": url} for label, url in links],
        "disclaimer": NOT_DIAGNOSIS,
    }


def _section(
    name: str, sources: Sequence[Source | None], ctx: _Context
) -> dict[str, Any]:
    used = [s for s in sources if s is not None]
    failed = [s for s in used if s.status != "ok"]
    section: dict[str, Any] = {
        "title": SECTIONS[name],
        "status": "ok" if len(failed) < len(used) else "failed" if used else "not_run",
        "summary": "",
        "findings": [],
        "missing": [],
        "warnings": [w for s in used for w in s.warnings()],
        "sources": [
            {
                "command": s.command,
                "status": s.status,
                "finished": s.finished or None,
                "manifest": ctx.rel(s.manifest),
            }
            for s in used
        ],
    }
    errors = [
        f"{s.command}: {(s.document.get('error') or {}).get('message', s.status)}"
        for s in failed
    ]
    if errors:
        section["error"] = "; ".join(errors)
    return section


def _missing(section: dict[str, Any], what: str, command: str) -> None:
    section["missing"].append({"what": what, "command": command})


def _quality_label(
    quality: Any, warnings: Sequence[Mapping[str, str]]
) -> tuple[str, list[str]]:
    if any(w["code"] == "low_coverage" for w in warnings):
        return variant_confidence.LOW, ["low_coverage"]
    if not isinstance(quality, (int, float)) or isinstance(quality, bool):
        return variant_confidence.MODERATE, ["quality_unknown"]
    if quality < LOW_QUALITY:
        return variant_confidence.LOW, ["low_caller_quality"]
    if quality < HIGH_QUALITY:
        return variant_confidence.MODERATE, ["moderate_caller_quality"]
    return variant_confidence.HIGH, []


def _overview(ctx: _Context) -> dict[str, Any]:
    info = latest(ctx.sources, _command("info"))
    section = _section("overview", [info], ctx)
    facts = []
    for name, label in _OVERVIEW_FACTS:
        fact = ctx.fact(name)
        if fact is None:
            continue
        value = fact.get("value")
        facts.append(
            {
                "name": name,
                "label": label,
                "value": value,
                "text": _fact_text(name, value),
                "source": fact.get("source"),
                "method": fact.get("method"),
                "confidence": fact.get("confidence"),
                "file": fact.get("file"),
                "at": fact.get("at"),
            }
        )
    if info is not None and info.status == "ok":
        # Outside a sample workspace nothing is recorded; `info` still says.
        known = {f["name"] for f in facts}
        for name, key in _INFO_FACTS:
            value = info.metrics.get(key)
            if name in known or value in (None, "", "Unknown"):
                continue
            facts.append(
                {
                    "name": name,
                    "label": dict(_OVERVIEW_FACTS)[name],
                    "value": value,
                    "text": _fact_text(name, value),
                    "source": "info",
                    "method": "info --detailed metrics (no sample workspace)",
                    "confidence": None,
                    "file": next(iter(map(ctx.rel, info.files("inputs"))), None),
                    "at": info.finished or None,
                }
            )
    section["facts"] = facts
    qc = []
    if info is not None and info.status == "ok":
        for key, label, unit in _QC_METRICS:
            value = info.metrics.get(key)
            if value in (None, ""):
                continue
            qc.append(
                {
                    "name": key,
                    "label": label,
                    "value": value,
                    "text": _number(value, unit),
                }
            )
    section["qc"] = qc
    if facts:
        section["status"] = "ok"
    if info is None or info.status != "ok":
        _missing(
            section,
            "QC metrics and sex (info --detailed)",
            ctx.command_for("info --detailed", ctx.alignment, "cram"),
        )
    section["summary"] = (
        ", ".join(f"{f['label']} {f['text']}" for f in facts[:4])
        or "No facts recorded."
    )
    return section


def _fact_text(name: str, value: Any) -> str:
    if isinstance(value, Mapping):
        return ", ".join(f"{k} {_number(v, 'x')}" for k, v in value.items())
    if name == "mean_coverage":
        return _number(value, "x")
    if name == "read_length":
        return _number(value, " bp")
    return str(value)


def _number(value: Any, unit: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    text = f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"
    return f"{text}{unit}"


def _lineage(ctx: _Context) -> dict[str, Any]:
    y = latest(ctx.sources, _command("lineage y-haplogroup"))
    mt = latest(ctx.sources, _command("lineage mt-haplogroup"))
    section = _section("lineage", [y, mt], ctx)
    from wgsextract_cli.commands._lineage_links import tree_links

    sex = ctx.fact("sex")
    female = bool(sex and str(sex.get("value")) == "Female")
    for source, what, mitochondrial, region in (
        (y, "Y-DNA haplogroup", False, "chrY"),
        (mt, "mtDNA haplogroup", True, "chrM"),
    ):
        command = "lineage mt-haplogroup" if mitochondrial else "lineage y-haplogroup"
        if source is None or source.status != "ok":
            if not mitochondrial and female:
                continue
            _missing(section, what, ctx.command_for(command, ctx.alignment, "cram"))
            continue
        haplogroup = str(source.metrics.get("haplogroup") or "")
        if not haplogroup:
            continue
        quality_key = "haplogroup_quality" if mitochondrial else "qc_score"
        quality = source.metrics.get(quality_key)
        label, reasons = _quality_label(quality, source.warnings())
        marker = source.metrics.get("haplogroup_marker")
        detail = ", ".join(
            x
            for x in (
                f"marker {marker}" if marker else "",
                f"quality {quality:g}" if isinstance(quality, (int, float)) else "",
            )
            if x
        )
        section["findings"].append(
            _finding(
                "lineage",
                what,
                haplogroup,
                detail=detail or None,
                confidence=label,
                reasons=reasons,
                basis="Haplogrep quality" if mitochondrial else "Yleaf QC-score",
                evidence=ctx.rel(source.output("haplogroup_report")),
                region=region,
                manifest=ctx.rel(source.manifest),
                links=tree_links(haplogroup, mitochondrial=mitochondrial),
            )
        )
    summary = "; ".join(f"{f['label']} {f['value']}" for f in section["findings"])
    if female and (y is None or y.status != "ok"):
        summary = "; ".join(
            x for x in (summary, "Y-DNA: none (recorded sex Female)") if x
        )
    section["summary"] = summary
    return section


def _labelled_calls(
    source: Source, role: str, table_role: str
) -> tuple[list[dict[str, Any]], bool]:
    """A run's labelled calls: the confidence TSV it wrote (every call), else
    the findings listed in its manifest (up to 1000), and whether that is
    every call."""
    table = source.output(table_role)
    if table is not None:
        try:
            with open(table, encoding="utf-8", newline="") as stream:
                lines = (line for line in stream if not line.startswith("#"))
                rows = list(csv.DictReader(lines, delimiter="\t"))
            for row in rows:
                row["reasons"] = [r for r in (row.get("reasons") or "").split("|") if r]
            return rows, True
        except (OSError, csv.Error):
            pass
    for entry in source.details.get("confidence") or []:
        if isinstance(entry, Mapping) and entry.get("role") == role:
            findings = [dict(f) for f in entry.get("findings") or []]
            return findings, not entry.get("findings_truncated")
    return [], True


def _worst(labels: Iterable[str]) -> str:
    order = {label: i for i, label in enumerate(variant_confidence.LABELS)}
    return max(labels, key=lambda label: order.get(label, -1))


def _pharmacogenomics(ctx: _Context) -> dict[str, Any]:
    pharmcat = latest(
        ctx.sources, _command_or_role("vcf pharmcat", "pharmcat_phenotypes")
    )
    cyrius = latest(ctx.sources, _command("typing cyp2d6"))
    hla = latest(ctx.sources, _command("typing hla"))
    section = _section("pharmacogenomics", [pharmcat, cyrius, hla], ctx)
    if pharmcat is not None and pharmcat.status == "ok":
        _pharmcat_findings(section, pharmcat, ctx)
    else:
        _missing(
            section,
            "PharmCAT diplotypes and phenotypes (GRCh38 VCF)",
            ctx.command_for("vcf pharmcat", ctx.vcf, "vcf.gz"),
        )
    if cyrius is not None and cyrius.status == "ok":
        _cyrius_finding(section, cyrius, ctx)
    else:
        _missing(
            section,
            "CYP2D6 star alleles (Cyrius, ~30x WGS)",
            ctx.command_for("typing cyp2d6", ctx.alignment, "cram"),
        )
    if hla is not None and hla.status == "ok":
        _hla_findings(section, hla, ctx)
    else:
        _missing(
            section,
            "HLA-A and HLA-B alleles (T1K)",
            ctx.command_for("typing hla", ctx.alignment, "cram"),
        )
    count = len(section["findings"])
    section["summary"] = f"{count} gene call{'s' * (count != 1)}." if count else ""
    return section


def _pharmcat_findings(section: dict[str, Any], source: Source, ctx: _Context) -> None:
    from wgsextract_cli.commands.vcf.pharmcat import summarize

    phenotypes = source.output("pharmcat_phenotypes")
    try:
        rows = summarize(str(phenotypes)) if phenotypes else []
    except (OSError, ValueError) as e:
        section["warnings"].append(
            {"code": "unreadable_evidence", "message": f"PharmCAT phenotypes: {e}"}
        )
        rows = []
    calls: dict[str, list[dict[str, Any]]] = {}
    for call in _labelled_calls(source, "pgx_calls", "confidence_table")[0]:
        for gene in str(call.get("gene") or "").split(","):
            if gene:
                calls.setdefault(gene, []).append(call)
    evidence = source.output("pharmcat_report", "pharmcat_phenotypes")
    for gene, diplotype, phenotype in rows:
        gene_calls = calls.get(gene, [])
        if gene_calls:
            label = _worst(str(c.get("confidence")) for c in gene_calls)
            reasons = sorted(
                {
                    r
                    for c in gene_calls
                    if c.get("confidence") == label
                    for r in c["reasons"]
                }
            )
            basis = f"weakest of {len(gene_calls)} labelled call(s) in the gene"
        else:
            label, reasons = NOT_ASSESSED, ["no_labelled_calls"]
            basis = "no labelled calls in the gene"
        section["findings"].append(
            _finding(
                "pharmacogenomics",
                f"{gene} diplotype (PharmCAT)",
                diplotype,
                detail=phenotype if phenotype != "-" else None,
                gene=gene,
                confidence=label,
                reasons=reasons,
                basis=basis,
                evidence=ctx.rel(evidence),
                region=f"gene {gene}",
                manifest=ctx.rel(source.manifest),
            )
        )


def _cyrius_finding(section: dict[str, Any], source: Source, ctx: _Context) -> None:
    metrics = source.metrics
    genotype = metrics.get("cyp2d6_genotype")
    status = metrics.get("cyp2d6_filter")
    depth = metrics.get("median_depth")
    thresholds = variant_confidence.thresholds_from()
    if not genotype:
        label, reasons = variant_confidence.LOW, ["no_call"]
    elif status != "PASS":
        label, reasons = variant_confidence.LOW, ["caller_filter"]
    elif not isinstance(depth, (int, float)):
        label, reasons = variant_confidence.MODERATE, ["depth_unknown"]
    elif depth < thresholds.min_depth:
        label, reasons = variant_confidence.LOW, ["low_depth"]
    elif depth < thresholds.high_depth:
        label, reasons = variant_confidence.MODERATE, ["moderate_depth"]
    else:
        label, reasons = variant_confidence.HIGH, []
    copies = metrics.get("cyp2d6_copy_number")
    detail = ", ".join(
        x
        for x in (
            f"filter {status}" if status else "",
            f"{copies} copies" if copies is not None else "",
            f"median depth {_number(depth, 'x')}" if depth is not None else "",
        )
        if x
    )
    section["findings"].append(
        _finding(
            "pharmacogenomics",
            "CYP2D6 diplotype (Cyrius)",
            str(genotype or "no call"),
            detail=detail or None,
            gene="CYP2D6",
            confidence=label,
            reasons=reasons,
            basis="Cyrius filter and median depth vs. confidence thresholds",
            evidence=ctx.rel(source.output("cyp2d6_calls", "cyrius_report")),
            region="gene CYP2D6",
            manifest=ctx.rel(source.manifest),
        )
    )


def _hla_findings(section: dict[str, Any], source: Source, ctx: _Context) -> None:
    """The HLA-A and HLA-B diplotypes ``typing hla`` called (those PharmCAT
    takes), labelled by T1K's allele qualities."""
    from wgsextract_cli.commands.genotyping.hla import (
        PHARMCAT_GENES,
        parse_t1k,
        reported_diplotype,
    )

    evidence = source.output("hla_genotypes")
    try:
        calls = {c.gene: c for c in parse_t1k(str(evidence))} if evidence else None
    except (OSError, ValueError, IndexError) as e:
        section["warnings"].append(
            {"code": "unreadable_evidence", "message": f"T1K genotypes: {e}"}
        )
        calls = None
    release = source.metrics.get("ipd_imgt_hla_release")
    for gene in PHARMCAT_GENES:
        diplotype = source.metrics.get(gene)
        call = (calls or {}).get(gene)
        qualities = [q for _, q in call.alleles] if call else []
        if calls is not None and reported_diplotype(call) != diplotype:
            # e.g. a later, interrupted run replaced the genotypes
            label, reasons = NOT_ASSESSED, ["evidence_mismatch"]
            section["warnings"].append(
                {
                    "code": "evidence_mismatch",
                    "message": f"The T1K genotypes no longer give the {gene} call "
                    "typing hla recorded; rerun it.",
                }
            )
        elif not diplotype:
            label, reasons = variant_confidence.LOW, ["no_call"]
        elif calls is None:
            label, reasons = variant_confidence.MODERATE, ["quality_unknown"]
        else:
            label, reasons = variant_confidence.HIGH, []
        detail = "; ".join(
            x
            for x in (
                f"T1K quality {', '.join(map(str, qualities))}" if qualities else "",
                str(release or ""),
            )
            if x
        )
        section["findings"].append(
            _finding(
                "pharmacogenomics",
                f"{gene} alleles (T1K)",
                str(diplotype or "no confident call"),
                detail=detail or None,
                gene=gene,
                confidence=label,
                reasons=reasons,
                basis="T1K allele quality (above 0 for each allele, as T1K recommends)",
                evidence=ctx.rel(evidence),
                region=f"gene {gene}",
                manifest=ctx.rel(source.manifest),
            )
        )


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clinvar(ctx: _Context) -> dict[str, Any]:
    source = latest(ctx.sources, _command_or_role("vcf clinvar", "clinvar_pathogenic"))
    section = _section("clinvar", [source], ctx)
    described = gene_lists.describe(ctx.gene_list)
    section["title"] = SECTIONS["clinvar"].format(gene_list=described["name"])
    section["gene_list"] = described
    if source is None or source.status != "ok":
        command = ctx.command_for("vcf clinvar", ctx.vcf, "vcf.gz")
        if ctx.vcf is None and ctx.alignment:
            command = (
                f"{ctx.command_for('vcf snp', ctx.alignment, 'cram')}, then {command}"
            )
        _missing(section, "ClinVar annotation (vcf clinvar)", command)
        return section
    vcf = source.output("clinvar_pathogenic")
    # The evidence VCF's own header, else the current facts of the VCF it read
    vcf_inputs = [
        source.locate(str(r["path"]))
        for role in ("vcf_input", "input")
        for r in source.records("inputs")
        if r.get("role") == role
        and str(r.get("path") or "").lower().endswith(_VCF_SUFFIXES)
        and read_scope.may_follow(source.locate(str(r["path"])))
    ]
    build = _gene_build(
        (_vcf_build(vcf) if vcf else None)
        or next(filter(None, map(ctx.build_of, vcf_inputs)), None)
    )
    labelled, complete = _labelled_calls(
        source, "clinvar_pathogenic", "confidence_table"
    )
    calls = [
        c
        for c in labelled
        if variant_confidence.is_pathogenic(str(c.get("CLNSIG") or ""))
    ]
    gene_list = gene_lists.GeneList(described["id"], described["name"], ())
    unavailable = False
    if calls:
        try:
            gene_list = gene_lists.load(ctx.gene_list)
        except gene_lists.GeneListUnavailable as error:
            unavailable = True
            section["warnings"].append(
                {"code": "gene_list_unavailable", "message": str(error)}
            )
    outside = unplaced = 0
    findings = []
    gene_calls: dict[str, tuple[gene_lists.Gene, list[Mapping[str, Any]]]] = {}
    for call in () if unavailable else calls:
        pos = _int(call.get("pos"))
        chrom = str(call.get("chrom") or "")
        genes = _call_genes(gene_list, call, build, chrom, pos)
        if genes is None:
            unplaced += 1
            continue
        if not genes:
            outside += 1
            continue
        for gene in genes:
            gene_calls.setdefault(gene.symbol, (gene, []))[1].append(call)
        significance = str(call.get("CLNSIG") or "").replace("_", " ")
        # A variant ClinVar places in several listed genes is one finding.
        symbols = "/".join(g.symbol for g in genes)
        notes = [
            f"{g.symbol}: {g.note}" if len(genes) > 1 else g.note
            for g in genes
            if g.note
        ]
        findings.append(
            _finding(
                "clinvar",
                f"{symbols} {significance}",
                significance,
                detail=str(call.get("CLNDN") or "").replace("_", " ") or None,
                gene=symbols,
                note="; ".join(notes) or None,
                confidence=str(call.get("confidence") or NOT_ASSESSED),
                reasons=list(call.get("reasons") or []),
                basis=variant_confidence.SCHEME,
                evidence=ctx.rel(vcf),
                region=f"{chrom}:{pos}",
                manifest=ctx.rel(source.manifest),
                variant={
                    "chrom": chrom,
                    "pos": pos,
                    "ref": call.get("ref"),
                    "alt": call.get("alt"),
                    "genotype": call.get("genotype"),
                    "zygosity": call.get("zygosity"),
                    "depth": _int(call.get("depth")),
                    "genotype_quality": _int(call.get("genotype_quality")),
                    "alt_fraction": _float(call.get("alt_fraction")),
                },
            )
        )
    order = {label: i for i, label in enumerate(CONFIDENCE)}
    findings.sort(key=lambda f: (order.get(f["confidence"], 9), f["gene"] or ""))
    section["findings"] = findings
    section["genes"] = sorted(
        (_gene_calls(g, c, complete) for g, c in gene_calls.values()),
        key=lambda g: g["gene"],
    )
    if not complete:
        section["warnings"].append(
            {
                "code": "calls_truncated",
                "message": "The run's confidence table is missing, and its "
                "manifest lists only some calls: gene counts may be incomplete, "
                "and only a biallelic status (which more calls can't change) is "
                "given. Rerun vcf clinvar.",
            }
        )
    section["build"] = build
    section["counts"] = {
        "carried": len(calls),
        "in_gene_list": len(findings),
        "outside_gene_list": outside,
        "unplaced": unplaced,
        **{
            label: sum(f["confidence"] == label for f in findings)
            for label in variant_confidence.LABELS
        },
    }
    section["all_calls"] = ctx.rel(source.output("confidence_table") or vcf)
    if unavailable:
        section["counts"]["unplaced"] = len(calls)
        section["summary"] = (
            f"{len(calls)} call{'s' * (len(calls) != 1)} not placed in genes: "
            "the gene list is unavailable; see the full table."
        )
        return section
    if unplaced and build is None and gene_list.needs_build():
        section["warnings"].append(
            {
                "code": "build_unknown",
                "message": "The VCF's build could not be identified, so its calls "
                "could not be placed in genes; see the full table.",
            }
        )
    elif unplaced:
        section["warnings"].append(
            {
                "code": "gene_annotation_missing",
                "message": f"{unplaced} call{'s' * (unplaced != 1)} have no ClinVar "
                "GENEINFO and the gene list has no spans to place them by; rerun "
                "vcf clinvar with a current ClinVar file, or use a list with "
                "spans. See the full table.",
            }
        )
    flagged = [
        f"{g['gene']} {g['status'].replace('_', ' ')}"
        + ("" if g["confidence"] == "high" else f" ({_confidence_text(g)})")
        for g in section["genes"]
        if g["status"]
    ]
    section["summary"] = (
        f"{len(findings)} call{'s' * (len(findings) != 1)} in "
        f"{described['name']} genes; {outside} more elsewhere"
        + (f"; {unplaced} not placed" if unplaced else "")
        + (f". Recessive genes: {', '.join(flagged)}" if flagged else "")
        + "."
    )
    return section


# A recessive gene's P/LP calls: one copy affected, (maybe) both.
GENE_STATUS = {
    "biallelic": "a homozygous P/LP call: both copies affected",
    "possible_biallelic": "possibly both copies affected: two or more "
    "heterozygous P/LP calls whose phase is unknown (they may be on the same "
    "copy), or a call whose other allele was not called",
    "carrier": "one heterozygous P/LP call: one copy affected",
}


def _gene_calls(
    gene: gene_lists.Gene, calls: Sequence[Mapping[str, Any]], complete: bool = True
) -> dict[str, Any]:
    """A listed gene's P/LP calls by zygosity, and for a gene the list reports
    only when biallelic (AR) whether this sample is a carrier or (possibly)
    biallelic, with the weakest confidence of those calls. Phase is not
    known, and a haploid or partly called genotype (``1/.``) doesn't show the
    other copy, so these make the gene only possibly biallelic."""
    het = hom = hemi = partial = 0
    for call in calls:
        zygosity = str(call.get("zygosity") or "")
        alleles = re.split(r"[/|]", str(call.get("genotype") or ""))
        if zygosity == "het" and "." in alleles:
            partial += 1
        elif zygosity == "het":
            het += 1
        elif zygosity == "hom":
            hom += 1
        elif zygosity == "haploid":
            hemi += 1
    status = None
    if gene.recessive:
        if hom:
            status = "biallelic"
        elif hemi or partial or het >= 2:
            status = "possible_biallelic"
        elif het:
            status = "carrier"
    if not complete and status != "biallelic":
        status = None
    order = {label: i for i, label in enumerate(CONFIDENCE)}
    labels = [str(c.get("confidence") or NOT_ASSESSED) for c in calls]
    return {
        "gene": gene.symbol,
        "inheritance": gene.inheritance or None,
        "heterozygous": het,
        "homozygous": hom,
        "hemizygous": hemi,
        "partial": partial,
        "confidence": max(labels, key=lambda label: order.get(label, len(order))),
        "status": status,
        "status_text": GENE_STATUS.get(status or "", ""),
    }


def _confidence_text(gene: Mapping[str, Any]) -> str:
    if gene["confidence"] == NOT_ASSESSED:
        return "calls not assessed"
    return f"{gene['confidence']}-confidence calls"


def _call_genes(
    gene_list: gene_lists.GeneList,
    call: Mapping[str, Any],
    build: str | None,
    chrom: str,
    pos: int | None,
) -> list[gene_lists.Gene] | None:
    """The list's genes ClinVar names for the call (GENEINFO), or, for an
    annotation without GENEINFO, those whose span holds it; None if neither
    can place the call."""
    if symbols := _geneinfo(call):
        return gene_list.named(symbols)
    if pos is None or not gene_list.has_spans(build):
        return None
    return gene_list.genes_at(build, chrom, pos)


def _geneinfo(call: Mapping[str, Any]) -> list[str]:
    """The gene symbols of ClinVar's GENEINFO (``SYMBOL:GeneID|...``)."""
    text = str(call.get("GENEINFO") or "")
    return [e.split(":")[0] for e in text.split("|") if e and e != "."]


def _microarray(ctx: _Context) -> dict[str, Any]:
    source = latest(ctx.sources, _command("microarray"))
    section = _section("microarray", [source], ctx)
    section["files"] = []
    if source is None or source.status != "ok":
        _missing(
            section,
            "Consumer microarray files (23andMe, AncestryDNA, ...)",
            ctx.command_for("microarray", ctx.alignment or ctx.vcf, "cram"),
        )
        return section
    for record in source.records("outputs"):
        if record.get("role") not in _MICROARRAY_ROLES or not record.get("path"):
            continue
        path = source.locate(str(record["path"]))
        metrics = record.get("metrics") or {}
        section["files"].append(
            {
                "file": ctx.rel(path),
                "role": record.get("role"),
                "format": metrics.get("format")
                or ("CombinedKit" if record.get("role") == "combined_kit" else None),
                "exists": path.exists(),
                "sites": metrics.get("sites"),
                "called": metrics.get("called"),
                "no_call_rate": metrics.get("no_call_rate"),
            }
        )
    section["metrics"] = {
        k: source.metrics[k]
        for k in ("autosomal_called", "autosomal_homozygosity")
        if k in source.metrics
    }
    count = len(section["files"])
    section["summary"] = f"{count} file{'s' * (count != 1)} produced."
    return section


def _choose_inputs(
    folder: Path,
    input_file: Path | None,
    sources: Sequence[Source],
    workspace: SampleWorkspace,
) -> tuple[str | None, str | None]:
    """The sample's alignment and VCF, relative to the folder, for hints and
    workspace facts: the given input, else what the runs read, else what the
    workspace recorded."""
    alignment = vcf = None
    candidates: list[Path] = []
    if input_file is not None:
        candidates.append(input_file)
    for source in reversed(sources):
        candidates.extend(
            source.locate(str(r["path"]))
            for r in source.records("inputs")
            if r.get("role") in ("input", "vcf_input") and r.get("path")
        )
    if input_file is None:
        keys = (workspace.describe().get("files") or {}).keys()
        candidates.extend(folder / key for key in keys)
    for path in candidates:
        if not _inside(path, folder) or not path.is_file():
            continue
        name = path.name.lower()
        if alignment is None and name.endswith(_ALIGNMENT_SUFFIXES):
            alignment = Path(os.path.relpath(path, folder)).as_posix()
        elif vcf is None and name.endswith(_VCF_SUFFIXES):
            vcf = Path(os.path.relpath(path, folder)).as_posix()
    return alignment, vcf


_POSIX_PATH = re.compile(r"""(?<![\w.~:/-])/(?:[^\s/:'"<>|]+/)+([^\s/:'"<>|]*)""")
_WINDOWS_PATH = re.compile(
    r"""(?<![\w])[A-Za-z]:[\\/](?:[^\s\\/:'"<>|]+[\\/])*([^\s\\/:'"<>|]*)"""
)


def _path_scrubber(ctx: _Context) -> Callable[[str], str]:
    """Rewrites messages from the runs for sharing: paths in the sample folder
    (as it is now, or as a manifest recorded it before a move) relative to
    it, and any other absolute path as its file name alone."""
    prefixes: dict[str, str] = {}
    for source in ctx.sources:
        here = ctx.rel(source.manifest.parent) or "."
        for folder in (source.recorded_folder, str(source.manifest.parent)):
            if folder:
                prefixes[folder] = here
    prefixes[str(ctx.folder)] = "."
    variants = {
        spelling: rel
        for folder, rel in prefixes.items()
        for spelling in {folder, folder.replace("\\", "/")}
        if Path(spelling).is_absolute() or PureWindowsPath(spelling).is_absolute()
    }
    ordered = sorted(variants.items(), key=lambda item: -len(item[0]))

    def scrub(text: str) -> str:
        for folder, rel in ordered:
            lead = "" if rel == "." else f"{rel}/"
            for sep in ("/", "\\"):
                text = text.replace(folder + sep, lead)
        text = _WINDOWS_PATH.sub(lambda m: m.group(1) or "…", text)
        return _POSIX_PATH.sub(lambda m: m.group(1) or "…", text)

    return scrub


def build_report(
    folder: Path,
    *,
    input_file: Path | None = None,
    results: Iterable[Path] = (),
    sources: list[Source] | None = None,
    gene_list: str | Path | None = None,
) -> dict[str, Any]:
    """The report document (``wgsextract.report/1``) of the sample in ``folder``.

    ``gene_list`` is a built-in list's id or a TSV or BED file (core/gene_lists.py);
    by default the newest ACMG SF list."""
    gene_lists.describe(gene_list)
    folder = Path(os.path.abspath(folder))
    if sources is None:
        sources = sample_sources(folder, input_file, results)
    workspace = SampleWorkspace(folder)
    alignment, vcf = _choose_inputs(folder, input_file, sources, workspace)
    main_file = alignment or vcf
    described = workspace.describe(folder / main_file if main_file else None)
    ctx = _Context(folder, sources, described, alignment, vcf, gene_list)
    info = latest(sources, _command("info"))
    name = (
        input_stem(alignment or vcf or "")
        or (
            input_stem(info.files("inputs")[0]) if info and info.files("inputs") else ""
        )
        or folder.name
    )
    sections = {
        "overview": _overview(ctx),
        "lineage": _lineage(ctx),
        "pharmacogenomics": _pharmacogenomics(ctx),
        "clinvar": _clinvar(ctx),
        "microarray": _microarray(ctx),
    }
    scrub = _path_scrubber(ctx)
    for section in sections.values():
        for warning in section["warnings"]:
            warning["message"] = scrub(warning["message"])
        if section.get("error"):
            section["error"] = scrub(section["error"])
        for finding in section["findings"]:
            for key in ("file", "manifest"):
                file = finding["evidence"][key]
                if file and not (folder / file).exists():
                    section["warnings"].append(
                        {
                            "code": "evidence_missing",
                            "message": f"Evidence file not found: {file}",
                        }
                    )
    return {
        "schema": SCHEMA_ID,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wgsextract_version": __version__,
        "sample": {
            "name": name,
            "folder": folder.name,
            "alignment": alignment,
            "vcf": vcf,
            "workspace": WORKSPACE_FILE if described.get("exists") else None,
        },
        "disclaimer": DISCLAIMER,
        "guide": GUIDE,
        "confidence_reasons": REASONS,
        "sections": sections,
    }


def findings(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [f for s in report["sections"].values() for f in s["findings"]]


def table_rows(report: Mapping[str, Any]) -> list[list[Value]]:
    rows: list[list[Value]] = []
    for f in findings(report):
        v = f["variant"] or {}
        rows.append(
            [
                f["section"],
                f["label"],
                f["value"],
                f["detail"],
                f["gene"],
                v.get("chrom"),
                v.get("pos"),
                v.get("ref"),
                v.get("alt"),
                v.get("genotype"),
                v.get("zygosity"),
                v.get("depth"),
                v.get("genotype_quality"),
                v.get("alt_fraction"),
                f["confidence"],
                "|".join(f["confidence_reasons"]),
                f["evidence"]["file"],
                f["evidence"]["region"],
                f["disclaimer"],
            ]
        )
    return rows


def load_schema() -> dict[str, Any]:
    """The wgsextract.report/1 JSON Schema shipped with the package."""
    text = resources.files("wgsextract_cli").joinpath(SCHEMA_RESOURCE).read_text()
    schema: dict[str, Any] = json.loads(text)
    return schema


# --- rendering -------------------------------------------------------------------

_CSS = """
body{font:13px/1.35 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:12px 16px;color:#1b1f24;max-width:1200px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:14px 0 4px;border-bottom:1px solid #d0d7de}
p{margin:3px 0}.muted,.nd{color:#57606a}.nd{font-size:11px;white-space:nowrap}
.disclaimer{background:#fff8c5;border:1px solid #d4a72c;padding:5px 8px;margin:6px 0}
table{border-collapse:collapse;margin:3px 0}
th,td{border:1px solid #d0d7de;padding:2px 5px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600}td.n{text-align:right;white-space:nowrap}
.conf{display:inline-block;padding:0 5px;border-radius:3px;font-size:11px;font-weight:600}
.high{background:#dafbe1;color:#116329}.moderate{background:#fff8c5;color:#7d4e00}
.low{background:#ffebe9;color:#a40e26}.not_assessed{background:#eaeef2;color:#424a53}
.status{font-size:11px;font-weight:600;padding:0 5px;border-radius:3px;background:#eaeef2}
.status.ok{background:#dafbe1}.status.failed{background:#ffebe9}
code{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#f6f8fa;padding:0 3px}
ul{margin:2px 0;padding-left:18px}
@media print{body{margin:0}a{color:inherit}}
""".strip()


def _href(file: str | None, base: str) -> str | None:
    if not file:
        return None
    for flavour in (PurePosixPath, PureWindowsPath):
        if flavour(file).is_absolute():  # evidence on another drive
            return flavour(file).as_uri()
    if base.startswith("file:"):
        return f"{base.rstrip('/')}/{quote(file)}"
    return quote(Path(os.path.normpath(Path(base, file))).as_posix())


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _link(text: str, href: str | None) -> str:
    return f'<a href="{_h(href)}">{_h(text)}</a>' if href else _h(text)


def render_html(report: Mapping[str, Any], base: str = ".") -> str:
    """A self-contained HTML page: inline CSS, no scripts, fonts or remote
    resources, so it renders offline. ``base`` is the sample folder relative
    to the page, for the evidence links."""
    sample = report["sample"]
    out = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>{_h(sample['name'])}: WGS Extract report</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{_h(sample['name'])}: WGS Extract report</h1>",
        f'<p class="muted">Generated {_h(report["generated"])} by wgsextract '
        f"{_h(report['wgsextract_version'])} from the results in "
        f"<code>{_h(sample['folder'])}</code>. Evidence links are relative to "
        "that folder.</p>",
        f'<p class="disclaimer"><b>{_h(NOT_DIAGNOSIS)}</b> '
        f"{_h(report['disclaimer'])}</p>",
    ]
    for name, section in report["sections"].items():
        out.append(
            f'<h2 id="{_h(name)}">{_h(section["title"])} '
            f'<span class="status {_h(section["status"])}">'
            f"{_h(section['status'].replace('_', ' '))}</span></h2>"
        )
        if section.get("summary"):
            out.append(f"<p>{_h(section['summary'])}</p>")
        if section.get("error"):
            out.append(f"<p><b>Failed:</b> {_h(section['error'])}</p>")
        out.extend(_html_section_body(name, section, base))
        if section["findings"]:
            out.append(_html_findings(section["findings"], report, base))
        if section["missing"]:
            out.append(
                '<p class="muted">Not run yet (run in the sample folder):</p><ul>'
            )
            out.extend(
                f"<li>{_h(m['what'])}: <code>{_h(m['command'])}</code></li>"
                for m in section["missing"]
            )
            out.append("</ul>")
        if section["warnings"]:
            out.append("<ul>")
            out.extend(
                f'<li class="muted">⚠ {_h(w["message"])}</li>'
                for w in section["warnings"]
            )
            out.append("</ul>")
        if section["sources"]:
            links = ", ".join(
                _link(f"{s['command']} ({s['status']})", _href(s["manifest"], base))
                for s in section["sources"]
            )
            out.append(f'<p class="muted">Sources: {links}</p>')
    out.append(
        f'<p class="muted">Confidence labels: {_h(report["guide"])} and '
        "docs/variant-confidence.md in WGS Extract.</p></body></html>"
    )
    return "\n".join(out) + "\n"


def _html_section_body(name: str, section: Mapping[str, Any], base: str) -> list[str]:
    out: list[str] = []
    if name == "overview":
        if section["facts"]:
            out.append("<table><tr><th>Fact</th><th>Value</th><th>Method</th>"
                       "<th>Confidence</th><th>From</th></tr>")  # fmt: skip
            for f in section["facts"]:
                method = "your override" if f["source"] == "override" else f["method"]
                out.append(
                    f"<tr><td>{_h(f['label'])}</td><td>{_h(f['text'])}</td>"
                    f"<td>{_h(method)}</td><td>{_h(f['confidence'] or 'not recorded')}</td>"
                    f"<td>{_h(f['file'])}</td></tr>"
                )
            out.append("</table>")
        if section["qc"]:
            cells = "".join(
                f"<tr><td>{_h(q['label'])}</td><td>{_h(q['text'])}</td></tr>"
                for q in section["qc"]
            )
            out.append(
                f"<table><tr><th>QC metric</th><th>Value</th></tr>{cells}</table>"
            )
    elif name == "clinvar":
        gl = section["gene_list"]
        source = [_h(gl["citation"])] if gl["citation"] else []
        source += [_link("Table 1", gl["url"])] if gl["url"] else []
        out.append(
            f'<p class="muted">Gene list: {_h(gl["name"])} ({gl["genes"]} genes'
            + (f"; {', '.join(source)}" if source else "")
            + ").</p>"
        )
        if section.get("all_calls"):
            out.append(
                f'<p class="muted">Every carried ClinVar P/LP call: '
                f"{_link(section['all_calls'], _href(section['all_calls'], base))}</p>"
            )
        if section.get("genes"):
            out.append("<table><tr><th>Gene</th><th>Inheritance</th>"
                       "<th>Heterozygous</th><th>Homozygous</th><th>Hemizygous</th>"
                       "<th>Partly called</th><th>Confidence</th>"
                       "<th>Recessive gene</th></tr>")  # fmt: skip
            for g in section["genes"]:
                status = (g["status"] or "").replace("_", " ")
                out.append(
                    f"<tr><td>{_h(g['gene'])}</td><td>{_h(g['inheritance'] or '')}</td>"
                    f'<td class="n">{g["heterozygous"]}</td>'
                    f'<td class="n">{g["homozygous"]}</td>'
                    f'<td class="n">{g["hemizygous"]}</td>'
                    f'<td class="n">{g["partial"]}</td>'
                    f"<td>{_h(g['confidence'])}</td><td>{_h(status)}"
                    + (
                        f'<br><span class="muted">{_h(g["status_text"])}</span>'
                        if status
                        else ""
                    )
                    + "</td></tr>"
                )
            out.append("</table>")
    elif name == "microarray" and section["files"]:
        out.append("<table><tr><th>File</th><th>Format</th><th>Sites</th>"
                   "<th>Called</th><th>No-call rate</th></tr>")  # fmt: skip
        for f in section["files"]:
            rate = f["no_call_rate"]
            out.append(
                f"<tr><td>{_link(f['file'], _href(f['file'], base))}"
                f"{'' if f['exists'] else ' (missing)'}</td><td>{_h(f['format'])}</td>"
                f'<td class="n">{_h(_number(f["sites"]) if f["sites"] is not None else "")}</td>'
                f'<td class="n">{_h(_number(f["called"]) if f["called"] is not None else "")}</td>'
                f'<td class="n">{_h(f"{rate:.1%}" if isinstance(rate, (int, float)) else "")}</td></tr>'
            )
        out.append("</table>")
    return out


def _html_findings(
    items: Sequence[Mapping[str, Any]], report: Mapping[str, Any], base: str
) -> str:
    reasons = report["confidence_reasons"]
    out = ["<table><tr><th>Finding</th><th>Result</th><th>Detail</th>"
           "<th>Call</th><th>Confidence</th><th>Evidence</th><th></th></tr>"]  # fmt: skip
    for f in items:
        v = f["variant"] or {}
        call = (
            f"{_h(v.get('ref'))}&gt;{_h(v.get('alt'))} {_h(v.get('genotype'))} "
            f"{_h(v.get('zygosity'))}, DP {_h(v.get('depth'))}, GQ "
            f"{_h(v.get('genotype_quality'))}"
            if v
            else ""
        )
        why = "; ".join(reasons.get(r, r) for r in f["confidence_reasons"])
        detail = _h(f["detail"]) + (f"<br><i>{_h(f['note'])}</i>" if f["note"] else "")
        links = " ".join(_link(x["label"], x["url"]) for x in f["links"])
        evidence = f["evidence"]
        out.append(
            f"<tr><td>{_h(f['label'])}</td><td>{_h(f['value'])}"
            f"{'<br>' + links if links else ''}</td><td>{detail}</td><td>{call}</td>"
            f'<td><span class="conf {_h(f["confidence"])}" title="{_h(why)}">'
            f"{_h(f['confidence'].replace('_', ' '))}</span>"
            f"{'<br><span class=muted>' + _h(why) + '</span>' if why else ''}</td>"
            f"<td>{_link(evidence['file'] or 'manifest', _href(evidence['file'] or evidence['manifest'], base))}"
            f"<br><code>{_h(evidence['region'])}</code></td>"
            f'<td class="nd">{_h(f["disclaimer"])}</td></tr>'
        )
    out.append("</table>")
    return "".join(out)


# Kept as is in Markdown link destinations; anything else is percent-encoded.
_URL_SAFE = ":/?#[]@!$&'()*+,;=%~"
_MD_ESCAPES = str.maketrans(
    {
        **{c: f"\\{c}" for c in "\\`*_[]|"},
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "\r": " ",
        "\n": " ",
    }
)


def _md(value: Any) -> str:
    """Text for Markdown (and a table cell): its markup, links and any HTML
    show as typed rather than being rendered."""
    return ("" if value is None else str(value)).translate(_MD_ESCAPES)


def _md_code(value: Any, *, cell: bool = False) -> str:
    """An inline code span (which shows its text as typed) that a backtick or
    line break, or in a table ``cell`` a pipe, in ``value`` cannot end."""
    text = " ".join(("" if value is None else str(value)).split())
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    if cell:
        text = text.replace("|", "\\|")
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_link(text: str, href: str | None) -> str:
    if not href:
        return _md(text)
    return f"[{_md(text)}](<{quote(href, safe=_URL_SAFE)}>)"


def render_markdown(report: Mapping[str, Any], base: str = ".") -> str:
    """The report as Markdown, with the same sections, findings and links."""
    sample = report["sample"]
    reasons = report["confidence_reasons"]
    out = [
        f"# {_md(sample['name'])}: WGS Extract report",
        "",
        f"Generated {_md(report['generated'])} by wgsextract "
        f"{_md(report['wgsextract_version'])} from the results in "
        f"{_md_code(sample['folder'])}. Evidence links are relative to that folder.",
        "",
        f"> **{_md(NOT_DIAGNOSIS)}** {_md(report['disclaimer'])}",
    ]
    for name, section in report["sections"].items():
        status = section["status"].replace("_", " ")
        out += ["", f"## {_md(section['title'])} ({_md(status)})"]
        if section.get("summary"):
            out += ["", _md(section["summary"])]
        if section.get("error"):
            out += ["", f"**Failed:** {_md(section['error'])}"]
        if name == "overview" and section["facts"]:
            out += [
                "",
                "| Fact | Value | Method | Confidence | From |",
                "|---|---|---|---|---|",
            ]
            for f in section["facts"]:
                method = "your override" if f["source"] == "override" else f["method"]
                out.append(
                    f"| {_md(f['label'])} | {_md(f['text'])} | {_md(method)} | "
                    f"{_md(f['confidence'] or 'not recorded')} | {_md(f['file'])} |"
                )
        if name == "overview" and section["qc"]:
            out += ["", "| QC metric | Value |", "|---|---|"]
            out += [f"| {_md(q['label'])} | {_md(q['text'])} |" for q in section["qc"]]
        if name == "clinvar":
            gl = section["gene_list"]
            source = [_md(gl["citation"])] if gl["citation"] else []
            source += [_md_link("Table 1", gl["url"])] if gl["url"] else []
            out += [
                "",
                f"Gene list: {_md(gl['name'])} ({_md(gl['genes'])} genes"
                + (f"; {', '.join(source)}" if source else "")
                + ").",
            ]
            if section.get("all_calls"):
                href = _href(section["all_calls"], base)
                out.append(
                    f"Every carried ClinVar P/LP call: {_md_link(section['all_calls'], href)}"
                )
            if section.get("genes"):
                out += [
                    "",
                    "| Gene | Inheritance | Heterozygous | Homozygous | Hemizygous "
                    "| Partly called | Confidence | Recessive gene |",
                    "|---|---|---|---|---|---|---|---|",
                ]
                for g in section["genes"]:
                    status = (g["status"] or "").replace("_", " ")
                    out.append(
                        f"| {_md(g['gene'])} | {_md(g['inheritance'])} | "
                        f"{g['heterozygous']} | {g['homozygous']} | {g['hemizygous']} | "
                        f"{g['partial']} | {_md(g['confidence'])} | "
                        + (
                            f"**{_md(status)}**: {_md(g['status_text'])}"
                            if status
                            else ""
                        )
                        + " |"
                    )
        if name == "microarray" and section["files"]:
            out += [
                "",
                "| File | Format | Sites | Called | No-call rate |",
                "|---|---|---|---|---|",
            ]
            for f in section["files"]:
                rate = f["no_call_rate"]
                out.append(
                    f"| {_md_link(f['file'], _href(f['file'], base))} | "
                    f"{_md(f['format'])} | {_md(f['sites'])} | {_md(f['called'])} | "
                    f"{f'{rate:.1%}' if isinstance(rate, (int, float)) else ''} |"
                )
        if section["findings"]:
            out += [
                "",
                "| Finding | Result | Detail | Call | Confidence | Evidence | |",
                "|---|---|---|---|---|---|---|",
            ]
            for f in section["findings"]:
                v = f["variant"] or {}
                call = (
                    f"{v.get('ref')}>{v.get('alt')} {v.get('genotype')} "
                    f"{v.get('zygosity')}, DP {v.get('depth')}, GQ {v.get('genotype_quality')}"
                    if v
                    else ""
                )
                why = "; ".join(reasons.get(r, r) for r in f["confidence_reasons"])
                links = " ".join(_md_link(x["label"], x["url"]) for x in f["links"])
                detail = " — ".join(x for x in (f["detail"], f["note"]) if x)
                evidence = f["evidence"]
                target = evidence["file"] or evidence["manifest"]
                out.append(
                    f"| {_md(f['label'])} | {_md(f['value'])} {links} | {_md(detail)} | "
                    f"{_md(call)} | **{_md(f['confidence'])}**"
                    f"{' (' + _md(why) + ')' if why else ''} | "
                    f"{_md_link(evidence['file'] or 'manifest', _href(target, base))} "
                    f"{_md_code(evidence['region'], cell=True)} | {_md(f['disclaimer'])} |"
                )
        if section["missing"]:
            out += ["", "Not run yet (run in the sample folder):", ""]
            out += [
                f"- {_md(m['what'])}: {_md_code(m['command'])}"
                for m in section["missing"]
            ]
        if section["warnings"]:
            out.append("")
            out += [f"- ⚠ {_md(w['message'])}" for w in section["warnings"]]
        if section["sources"]:
            links = ", ".join(
                _md_link(f"{s['command']} ({s['status']})", _href(s["manifest"], base))
                for s in section["sources"]
            )
            out += ["", f"Sources: {links}"]
    out += [
        "",
        f"Confidence labels: {_md(report['guide'])} and docs/variant-confidence.md.",
    ]
    return "\n".join(out) + "\n"


def link_base(folder: Path, outdir: Path) -> str:
    """The sample folder relative to the report's folder, for its links."""
    try:
        return Path(
            os.path.relpath(os.path.abspath(folder), os.path.abspath(outdir))
        ).as_posix()
    except ValueError:
        return Path(os.path.abspath(folder)).as_uri()
