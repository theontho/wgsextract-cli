"""Confidence labels for the variant calls behind rare and clinical findings.

A ClinVar pathogenic match or a rare gnomAD allele is only as good as the call
under it, and a 30x genome has stretches at 5x, collapsed duplications and
strand-biased artifacts. Each carried ALT allele in a findings VCF gets
INFO/WGSE_CONF (high, moderate or low) and INFO/WGSE_CONF_REASONS (reason codes)
from the evidence the caller wrote for the sample: read depth, genotype
quality, allele balance, mapping quality, strand bias and FILTER. Every rule
and threshold, with its rationale and sources, is in docs/variant-confidence.md.

Labels are conservative screens of technical support, never a clinical
classification: a "high" call is still a research finding until an accredited
laboratory confirms it.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import logging
import math
import re
import signal
import subprocess
import threading
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from wgsextract_cli.core.atomic_output import atomic_output
from wgsextract_cli.core.config import KNOWN_SETTINGS, settings
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.results import Scalar, current_result, warn
from wgsextract_cli.core.utils import UsageError, WGSExtractError, popen
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

SCHEME = "wgsextract.confidence/1"
GUIDE = "docs/variant-confidence.md"
HIGH, MODERATE, LOW = "high", "moderate", "low"
LABELS = (HIGH, MODERATE, LOW)
CONF_TAG, REASONS_TAG = "WGSE_CONF", "WGSE_CONF_REASONS"
# Findings listed in the result manifest; the TSV always has all of them.
MAX_MANIFEST_FINDINGS = 1000
DISCLAIMER = (
    "Confidence labels describe the technical support for each call in this "
    "sequencing run, not whether a variant causes disease. Treat any finding as "
    "a research result: confirm it with an accredited clinical laboratory "
    "(e.g. a CLIA/CAP-certified test on a new sample) before acting on it."
)

# Fixed rule constants (docs/variant-confidence.md explains each).
HET_LOW_FRACTION = (0.2, 0.8)  # het alt fraction outside: low
HET_HIGH_FRACTION = (
    0.3,
    0.7,
)  # het alt fraction outside (but inside the above): moderate
HOM_LOW_FRACTION = 0.8  # hom/hemizygous alt fraction below: low
HOM_HIGH_FRACTION = 0.9  # ... below (but at least the above): moderate
# INFO/MQ below: low. RMS MQ (the VCF spec's MQ; GATK, DRAGEN) uses GATK's
# hard filter. A mean MQ (bcftools "Average mapping quality") sits well below
# the RMS of the same reads, so it uses MAPQ 20, a 1% chance of misplacement.
MIN_MAPPING_QUALITY = {"rms": 40.0, "mean": 20.0}
# INFO/QD below: low (GATK hard filter). Only GATK's QUAL is on the scale it
# assumes: DRAGEN's recalibrated QUAL puts half its good calls under QD 2.
MIN_QD = 2.0
MAX_FS = {"snv": 60.0, "indel": 200.0}  # INFO/FS above: low (GATK hard filter)
MAX_SOR = {"snv": 3.0, "indel": 10.0}  # SOR above: low (GATK hard filter)
SYMBOLIC_REFERENCE_ALTS = {"<*>", "<NON_REF>", "<X>"}
# INFO tag marking one ALT split from a multiallelic record (bcftools norm -m
# --old-rec-tag, vt decompose). Its PL is trimmed to REF/this ALT, dropping a
# called 1/2, and site-level DP4/SB still count every ALT's reads.
SPLIT_TAG = "OLD_MULTIALLELIC"

LOW_REASONS = {
    "filtered": "the caller's FILTER did not pass",
    "low_depth": "read depth below the minimum",
    "low_gq": "genotype quality below the minimum",
    "allele_imbalance": "alt read fraction far from the genotype's expectation",
    "low_mapq": "reads map ambiguously (low mean mapping quality)",
    "strand_bias": "alt reads come mostly from one strand",
    "low_qd": "variant quality is low for its depth (QD below 2)",
    "no_call_metrics": "no depth, genotype quality or allele counts to judge by",
}
MODERATE_REASONS = {
    "moderate_depth": "read depth below the high-confidence level",
    "moderate_gq": "genotype quality below the high-confidence level",
    "skewed_allele_balance": "alt read fraction somewhat off the expectation",
    "depth_unknown": "the VCF has no read depth for the call",
    "gq_unknown": "the VCF has no genotype quality or likelihoods for the call "
    "(a split multiallelic record's likelihoods no longer cover it)",
    "allele_balance_unknown": "the VCF has no allele counts for the call "
    "(a split multiallelic record's site counts mix every ALT)",
}
REASONS = {**LOW_REASONS, **MODERATE_REASONS}


@dataclass(frozen=True)
class Thresholds:
    min_depth: float
    high_depth: float
    min_gq: float
    high_gq: float

    def __post_init__(self) -> None:
        if self.high_depth < self.min_depth or self.high_gq < self.min_gq:
            raise UsageError(
                "Confidence thresholds need LOW <= HIGH (depth "
                f"{self.min_depth:g}/{self.high_depth:g}, genotype quality "
                f"{self.min_gq:g}/{self.high_gq:g})."
            )


def _setting(key: str) -> float:
    value = settings.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        value = KNOWN_SETTINGS[key][0]
    return float(value) if isinstance(value, int | float) else 0.0


def thresholds_from(args: argparse.Namespace | None = None) -> Thresholds:
    """Thresholds from config.toml, overridden by --confidence-depth/-gq."""
    depth = getattr(args, "confidence_depth", None)
    gq = getattr(args, "confidence_gq", None)
    return Thresholds(
        min_depth=depth[0] if depth else _setting("confidence_min_depth"),
        high_depth=depth[1] if depth else _setting("confidence_high_depth"),
        min_gq=gq[0] if gq else _setting("confidence_min_gq"),
        high_gq=gq[1] if gq else _setting("confidence_high_gq"),
    )


def _non_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative number: {text!r}")
    return value


def add_confidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confidence-depth",
        nargs=2,
        type=_non_negative,
        metavar=("LOW", "HIGH"),
        help="Read depth below LOW labels a call low confidence, below HIGH "
        "moderate (default: config confidence_min_depth 10, "
        "confidence_high_depth 20; halved for haploid calls)",
    )
    parser.add_argument(
        "--confidence-gq",
        nargs=2,
        type=_non_negative,
        metavar=("LOW", "HIGH"),
        help="Genotype quality below LOW labels a call low confidence, below "
        "HIGH moderate (default: config confidence_min_gq 20, "
        "confidence_high_gq 30)",
    )


_CALLER_MARKERS = (
    ("deepvariant", re.compile(r"^##DeepVariant_version", re.I)),
    ("dragen", re.compile(r"^##(DRAGENCommandLine|source=DRAGEN)", re.I)),
    ("clair3", re.compile(r"^##source=Clair3|^##clair3", re.I)),
    (
        "gatk",
        re.compile(
            r"^##(GATKCommandLine|source=(HaplotypeCaller|GenotypeGVCFs|Mutect2))", re.I
        ),
    ),  # fmt: skip
    ("freebayes", re.compile(r"^##source=freeBayes", re.I)),
    ("strelka", re.compile(r"^##source=strelka", re.I)),
    ("bcftools", re.compile(r"^##bcftools_callCommand")),
)


def mapping_quality_kind(header_lines: Iterable[str]) -> str:
    """ "mean" when the header declares INFO/MQ as an average, else "rms"."""
    for line in header_lines:
        if line.startswith("##INFO=<ID=MQ,"):
            text = line.lower()
            rms = "rms" in text or "root mean square" in text
            if not rms and ("average" in text or "mean" in text):
                return "mean"
    return "rms"


def detect_caller(header_lines: Iterable[str]) -> str:
    """The variant caller named in a VCF header, or "unknown"."""
    lines = list(header_lines)
    for caller, marker in _CALLER_MARKERS:
        if any(marker.match(line) for line in lines):
            return caller
    return "unknown"


def _number(value: str | None) -> float | None:
    if value is None or value in {"", "."}:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _numbers(value: str | None) -> list[float | None]:
    if value is None or value in {"", "."}:
        return []
    return [_number(part) for part in value.split(",")]


def strand_odds_ratio(ref_fwd: float, ref_rev: float, alt_fwd: float, alt_rev: float) -> float:  # fmt: skip
    """GATK's StrandOddsRatio from forward/reverse ref and alt counts."""
    rf, rr, af, ar = ref_fwd + 1, ref_rev + 1, alt_fwd + 1, alt_rev + 1
    ratio = (rf * ar) / (rr * af)
    return (
        math.log(ratio + 1 / ratio)
        + math.log(min(rf, rr) / max(rf, rr))
        - math.log(min(af, ar) / max(af, ar))
    )


def _genotype_quality(fmt: Mapping[str, str], split: bool) -> float | None:
    gq = _number(fmt.get("GQ"))
    if gq is not None or split:
        return gq
    likelihoods = sorted(v for v in _numbers(fmt.get("PL")) if v is not None)
    if len(likelihoods) >= 2:
        return min(likelihoods[1] - likelihoods[0], 99.0)
    return None


def _depth(
    fmt: Mapping[str, str], info: Mapping[str, str], split: bool
) -> float | None:
    depth = _number(fmt.get("DP"))
    if depth is not None:
        return depth
    # A split record's AD keeps only REF and its own ALT.
    counts = [] if split else [v for v in _numbers(fmt.get("AD")) if v is not None]
    if counts:
        return math.fsum(counts)
    # INFO/DP is the site's depth over every sample: only one sample is judged.
    return _number(info.get("DP"))


def _alt_fraction(
    alt: int, n_alts: int, fmt: Mapping[str, str], info: Mapping[str, str], split: bool
) -> float | None:
    counts = _numbers(fmt.get("AD"))
    alt_count = counts[alt] if len(counts) > alt else None
    format_depth = _number(fmt.get("DP"))
    # A split record's AD keeps only REF and this ALT: only FORMAT/DP still
    # counts the reads of the ALTs split off.
    if alt_count is not None and (format_depth is not None or not split):
        total = max(math.fsum(v or 0.0 for v in counts), format_depth or 0)
        return alt_count / total if total else None
    for key in ("VAF", "AF"):  # DeepVariant VAF, Clair3 AF: per ALT
        fractions = _numbers(fmt.get(key))
        if len(fractions) >= alt and fractions[alt - 1] is not None:
            return fractions[alt - 1]
    dp4 = [v or 0.0 for v in _numbers(info.get("DP4"))]
    if n_alts == 1 and not split and len(dp4) == 4 and math.fsum(dp4):
        return (dp4[2] + dp4[3]) / math.fsum(dp4)
    return None


def _strand_counts(
    fmt: Mapping[str, str], info: Mapping[str, str], n_alts: int, split: bool
) -> list[float] | None:
    for counts in (_numbers(fmt.get("SB")), _numbers(info.get("DP4"))):
        if (
            n_alts == 1
            and not split
            and len(counts) == 4
            and all(v is not None for v in counts)
        ):
            return [v or 0.0 for v in counts]
    return None


@dataclass
class CallConfidence:
    label: str
    reasons: list[str]
    zygosity: str
    depth: float | None
    genotype_quality: float | None
    alt_fraction: float | None


def classify(
    *,
    alt: int,
    alleles: Sequence[str],
    ref: str,
    alt_allele: str,
    filters: str,
    info: Mapping[str, str],
    fmt: Mapping[str, str],
    n_alts: int,
    thresholds: Thresholds,
    mq_kind: str = "rms",
    caller: str = "unknown",
    samples: int = 1,
) -> CallConfidence | None:
    """The confidence of ALT number ``alt`` (1-based), or None when the sample
    doesn't carry it."""
    copies = sum(allele == str(alt) for allele in alleles)
    if not copies:
        return None
    zygosity = (
        "haploid" if len(alleles) == 1 else ("hom" if copies == len(alleles) else "het")
    )
    kind = "snv" if len(ref) == len(alt_allele) == 1 else "indel"
    low: list[str] = []
    moderate: list[str] = []

    if filters not in {"PASS", "."}:
        low.append("filtered")

    split = SPLIT_TAG in info
    # INFO/DP and DP4 pool every sample's reads: they are this call's evidence
    # only in a single-sample VCF.
    counts_info = info if samples == 1 else {}
    scale = 0.5 if zygosity == "haploid" else 1.0
    depth = _depth(fmt, counts_info, split)
    if depth is None:
        moderate.append("depth_unknown")
    elif depth < thresholds.min_depth * scale:
        low.append("low_depth")
    elif depth < thresholds.high_depth * scale:
        moderate.append("moderate_depth")

    gq = _genotype_quality(fmt, split)
    if gq is None:
        moderate.append("gq_unknown")
    elif gq < thresholds.min_gq:
        low.append("low_gq")
    elif gq < thresholds.high_gq:
        moderate.append("moderate_gq")

    fraction = _alt_fraction(alt, n_alts, fmt, counts_info, split)
    if fraction is None:
        moderate.append("allele_balance_unknown")
    elif zygosity == "het":
        if not HET_LOW_FRACTION[0] <= fraction <= HET_LOW_FRACTION[1]:
            low.append("allele_imbalance")
        elif not HET_HIGH_FRACTION[0] <= fraction <= HET_HIGH_FRACTION[1]:
            moderate.append("skewed_allele_balance")
    elif fraction < HOM_LOW_FRACTION:
        low.append("allele_imbalance")
    elif fraction < HOM_HIGH_FRACTION:
        moderate.append("skewed_allele_balance")

    if depth is None and gq is None and fraction is None:
        low.append("no_call_metrics")

    mq = _number(info.get("MQ"))
    if mq is not None and mq < MIN_MAPPING_QUALITY[mq_kind]:
        low.append("low_mapq")

    fs, sor = _number(info.get("FS")), _number(info.get("SOR"))
    if sor is None and fs is None:
        counts = _strand_counts(fmt, counts_info, n_alts, split)
        sor = strand_odds_ratio(*counts) if counts else None
    if (fs is not None and fs > MAX_FS[kind]) or (
        sor is not None and sor > MAX_SOR[kind]
    ):
        low.append("strand_bias")

    qd = _number(info.get("QD")) if caller == "gatk" else None
    if qd is not None and qd < MIN_QD:
        low.append("low_qd")

    label = LOW if low else MODERATE if moderate else HIGH
    return CallConfidence(label, low + moderate, zygosity, depth, gq, fraction)


@dataclass
class Finding:
    chrom: str
    pos: int
    ref: str
    alt: str
    genotype: str
    confidence: CallConfidence
    annotations: dict[str, str] = field(default_factory=dict)

    def row(self, columns: Sequence[str]) -> dict[str, str]:
        c = self.confidence
        return {
            "chrom": self.chrom,
            "pos": str(self.pos),
            "ref": self.ref,
            "alt": self.alt,
            "genotype": self.genotype,
            "zygosity": c.zygosity,
            "confidence": c.label,
            "reasons": "|".join(c.reasons),
            "depth": _format(c.depth),
            "genotype_quality": _format(c.genotype_quality),
            "alt_fraction": _format(c.alt_fraction, 3),
            **{column: self.annotations.get(column, "") for column in columns},
        }

    def manifest(self) -> dict[str, Any]:
        c = self.confidence
        return {
            "chrom": self.chrom,
            "pos": self.pos,
            "ref": self.ref,
            "alt": self.alt,
            "genotype": self.genotype,
            "zygosity": c.zygosity,
            "confidence": c.label,
            "reasons": list(c.reasons),
            "depth": c.depth,
            "genotype_quality": c.genotype_quality,
            "alt_fraction": None
            if c.alt_fraction is None
            else round(c.alt_fraction, 4),
            **{k: v for k, v in self.annotations.items() if v},
        }


def _format(value: float | None, digits: int = 0) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}" if digits else f"{value:g}"


@dataclass
class ConfidenceSummary:
    caller: str
    thresholds: Thresholds
    sample: str | None = None
    records: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    not_carried: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def carried(self) -> int:
        return sum(self.counts[label] for label in LABELS)

    def metrics(self, prefix: str = "confidence") -> dict[str, Scalar]:
        return {
            **{f"{prefix}_{label}": self.counts[label] for label in LABELS},
            f"{prefix}_not_carried": self.not_carried,
        }

    def details(self, output: str | None, role: str) -> dict[str, Any]:
        listed = self.findings[:MAX_MANIFEST_FINDINGS]
        return {
            "scheme": SCHEME,
            "role": role,
            "output": None if output is None else str(Path(output).absolute()),
            "sample": self.sample,
            "caller": self.caller,
            "thresholds": asdict(self.thresholds),
            "counts": {label: self.counts[label] for label in LABELS},
            "not_carried": self.not_carried,
            "reasons": dict(sorted(self.reasons.items())),
            "findings": [finding.manifest() for finding in listed],
            "findings_truncated": len(self.findings) > len(listed),
            "disclaimer": DISCLAIMER,
            "guide": GUIDE,
        }


def _header_lines(thresholds: Thresholds, caller: str) -> list[str]:
    t = thresholds
    return [
        f'##INFO=<ID={CONF_TAG},Number=A,Type=String,Description="wgsextract '
        f"confidence in the sample's call of this ALT: high, moderate or low "
        f'({GUIDE}); . when the sample does not carry it">',
        f'##INFO=<ID={REASONS_TAG},Number=A,Type=String,Description="Reason codes '
        f'behind {CONF_TAG}, joined by | ({GUIDE})">',
        f"##wgsextract_confidence=<Scheme={SCHEME},Caller={caller},"
        f"MinDepth={t.min_depth:g},HighDepth={t.high_depth:g},"
        f"MinGQ={t.min_gq:g},HighGQ={t.high_gq:g}>",
    ]


_OWN_HEADER = re.compile(
    rf"^##(INFO=<ID=({CONF_TAG}|{REASONS_TAG}),|wgsextract_confidence=)"
)


def _strip_own_info(info: str) -> str:
    kept = [
        item
        for item in info.split(";")
        if item.partition("=")[0] not in {CONF_TAG, REASONS_TAG}
    ]
    return ";".join(kept) or "."


def _parse_info(info: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if info == ".":
        return parsed
    for item in info.split(";"):
        key, _, value = item.partition("=")
        parsed[key] = value
    return parsed


def label_records(
    lines: Iterable[str],
    thresholds: Thresholds,
    *,
    summary_columns: Sequence[str] = (),
    region_names: Sequence[tuple[str, int, int, str]] = (),
    keep_findings: bool = True,
) -> Iterator[str | ConfidenceSummary]:
    """Label VCF text lines; yields output lines, then the summary last.

    ``summary_columns`` are INFO keys copied into each finding; region_names
    are (chrom, start0, end, name) intervals naming a finding's ``gene``.
    """
    header: list[str] = []
    summary: ConfidenceSummary | None = None
    mq_kind = "rms"
    for line in lines:
        line = line.rstrip("\r\n")
        if line.startswith("##"):
            if not _OWN_HEADER.match(line):
                header.append(line)
            continue
        if line.startswith("#"):
            columns = line.split("\t")
            caller = detect_caller(header)
            mq_kind = mapping_quality_kind(header)
            summary = ConfidenceSummary(
                caller, thresholds, sample=columns[9] if len(columns) > 9 else None
            )
            if len(columns) > 10:
                warn(
                    "confidence_first_sample_only",
                    f"Confidence labels judge only the first sample "
                    f"({columns[9]}) of this {len(columns) - 9}-sample VCF.",
                    sample=columns[9],
                )
            yield from header
            yield from _header_lines(thresholds, caller)
            yield line
            continue
        if summary is None:
            raise WGSExtractError("VCF records before the #CHROM header line.")
        summary.records += 1
        fields = line.split("\t")
        alts = fields[4].split(",")
        info = _parse_info(fields[7])
        fmt: dict[str, str] = {}
        alleles: list[str] = []
        if len(fields) > 9:
            fmt = dict(zip(fields[8].split(":"), fields[9].split(":"), strict=False))
            alleles = re.split(r"[/|]", fmt.get("GT", "."))
        labels: list[str] = []
        reasons: list[str] = []
        for index, alt_allele in enumerate(alts, start=1):
            result = (
                None
                if alt_allele in SYMBOLIC_REFERENCE_ALTS
                else classify(
                    alt=index,
                    alleles=alleles,
                    ref=fields[3],
                    alt_allele=alt_allele,
                    filters=fields[6],
                    info=info,
                    fmt=fmt,
                    n_alts=len(alts),
                    thresholds=thresholds,
                    mq_kind=mq_kind,
                    caller=summary.caller,
                    samples=max(len(fields) - 9, 1),
                )
            )
            if result is None:
                labels.append(".")
                reasons.append(".")
                if alt_allele not in SYMBOLIC_REFERENCE_ALTS:
                    summary.not_carried += 1
                continue
            labels.append(result.label)
            reasons.append("|".join(result.reasons) or ".")
            summary.counts[result.label] += 1
            summary.reasons.update(result.reasons)
            if keep_findings:
                annotations = {key: info.get(key, "") for key in summary_columns}
                if region_names:
                    pos = int(fields[1])
                    annotations["gene"] = ",".join(
                        name
                        for chrom, start, end, name in region_names
                        if chrom == fields[0] and start < pos <= end
                    )
                summary.findings.append(
                    Finding(
                        fields[0],
                        int(fields[1]),
                        fields[3],
                        alt_allele,
                        fmt.get("GT", "."),
                        result,
                        annotations,
                    )
                )
        fields[7] = _strip_own_info(fields[7])
        if any(label != "." for label in labels):
            tags = f"{CONF_TAG}={','.join(labels)};{REASONS_TAG}={','.join(reasons)}"
            fields[7] = tags if fields[7] == "." else f"{fields[7]};{tags}"
        yield "\t".join(fields)
    if summary is None:
        raise WGSExtractError("The VCF has no #CHROM header line.")
    yield summary


class _Stderr:
    """Drain a process's stderr in the background, so a chatty tool never
    blocks on a full pipe, keeping its tail for error messages."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.lines: deque[str] = deque(maxlen=20)
        self._thread = threading.Thread(
            target=self._drain, args=(process.stderr,), daemon=True
        )
        self._thread.start()

    def _drain(self, stream: IO[str] | None) -> None:
        if stream is not None:
            for line in stream:
                self.lines.append(line.rstrip())

    def tail(self) -> str:
        self._thread.join(timeout=5)
        return "\n".join(self.lines)


def _start(
    command: Sequence[str], **kwargs: Any
) -> tuple[subprocess.Popen[str], _Stderr]:
    process = popen(command, stderr=subprocess.PIPE, text=True, **kwargs)
    return process, _Stderr(process)


def label_vcf(
    source_vcf: PathInput,
    out_vcf: PathInput | None,
    thresholds: Thresholds,
    *,
    include: str | None = None,
    regions_file: PathInput | None = None,
    summary_columns: Sequence[str] = (),
    region_names: Sequence[tuple[str, int, int, str]] = (),
    keep_findings: bool = True,
) -> ConfidenceSummary:
    """Write source_vcf's records (those matching the bcftools ``include``
    expression, within ``regions_file``) with confidence labels to out_vcf,
    bgzipped and indexed; with out_vcf None, only summarise them."""
    view = ["bcftools", "view"]
    if include:
        view += ["-i", include]
    if regions_file:
        view += ["-R", str(regions_file)]
    reader, reader_errors = _start([*view, str(source_vcf)], stdout=subprocess.PIPE)
    assert reader.stdout is not None
    labelled = label_records(
        reader.stdout,
        thresholds,
        summary_columns=summary_columns,
        region_names=region_names,
        keep_findings=keep_findings,
    )

    def reader_failed() -> bool:
        return reader.wait() != 0

    def reader_error() -> WGSExtractError:
        return _ReaderError(
            f"bcftools failed reading {source_vcf}: {reader_errors.tail()}"
        )

    try:
        if out_vcf is None:
            summary = _write(labelled, None)
            if reader_failed():
                raise reader_error()
        else:
            with atomic_output(out_vcf) as staged, open(staged, "wb") as sink:
                summary = _write_bgzipped(labelled, sink, out_vcf)
                if reader_failed():
                    raise reader_error()
    except BaseException as error:
        # Closing the pipe stops a reader still writing (SIGPIPE), so the wait
        # can't hang. A reader that failed by itself explains malformed or
        # missing records better than the parse error does.
        reader.stdout.close()
        status = reader.wait()
        if status not in (0, -_SIGPIPE) and not isinstance(
            error, (_OutputError, _ReaderError, KeyboardInterrupt)
        ):
            raise reader_error() from error
        raise
    reader.stdout.close()
    if out_vcf is not None:
        ensure_vcf_indexed(out_vcf)
    return summary


_SIGPIPE = getattr(signal, "SIGPIPE", 13)


class _OutputError(WGSExtractError):
    """bgzip, not the reading bcftools, failed."""


class _ReaderError(WGSExtractError):
    """The reading bcftools failed."""


def _write_bgzipped(
    labelled: Iterable[str | ConfidenceSummary], sink: IO[bytes], out_vcf: PathInput
) -> ConfidenceSummary:
    writer, writer_errors = _start(["bgzip", "-c"], stdin=subprocess.PIPE, stdout=sink)
    assert writer.stdin is not None
    stopped = False
    try:
        summary = _write(labelled, writer.stdin)
    except OSError:
        stopped = True  # bgzip stopped reading: its status and stderr say why
    finally:
        with contextlib.suppress(OSError):
            writer.stdin.close()
        if writer.wait() != 0 or stopped:
            raise _OutputError(
                f"bgzip failed writing {out_vcf}: {writer_errors.tail()}"
            )
    return summary


def _write(
    labelled: Iterable[str | ConfidenceSummary], sink: IO[str] | None
) -> ConfidenceSummary:
    for item in labelled:
        if isinstance(item, ConfidenceSummary):
            return item
        if sink is not None:
            sink.write(item + "\n")
    raise WGSExtractError("Labelling produced no summary.")


TABLE_COLUMNS = (
    "chrom", "pos", "ref", "alt", "genotype", "zygosity", "confidence",
    "reasons", "depth", "genotype_quality", "alt_fraction",
)  # fmt: skip


def write_table(
    path: PathInput,
    summary: ConfidenceSummary,
    columns: Sequence[str] = (),
    title: str = "Variant calls",
) -> None:
    """A TSV of every labelled finding, headed by '#' comment lines that state
    the scheme, thresholds and the disclaimer."""
    t = summary.thresholds
    with atomic_output(path, retire_indexes=False) as staged:
        with open(staged, "w", newline="", encoding="utf-8") as handle:
            handle.write(
                f"# {title}: wgsextract confidence labels ({SCHEME}; {GUIDE})\n"
                f"# Caller: {summary.caller}; sample: {summary.sample or '.'}; "
                f"depth low<{t.min_depth:g} high>={t.high_depth:g}; GQ "
                f"low<{t.min_gq:g} high>={t.high_gq:g}\n"
                f"# {DISCLAIMER}\n"
            )
            writer = csv.DictWriter(
                handle,
                fieldnames=[*TABLE_COLUMNS, *columns],
                delimiter="\t",
                lineterminator="\n",
            )
            writer.writeheader()
            for finding in summary.findings:
                writer.writerow(finding.row(columns))


def _counts_text(summary: ConfidenceSummary) -> str:
    return ", ".join(f"{summary.counts[label]} {label}" for label in LABELS)


def record(
    summary: ConfidenceSummary,
    *,
    output: PathInput | None,
    role: str,
    what: str,
    table: PathInput | None = None,
    clinical: int = 0,
) -> None:
    """Log the counts and add them, warnings and details to the result manifest.

    ``what`` names the findings in messages ("ClinVar pathogenic calls");
    ``clinical`` counts the carried ClinVar pathogenic/likely pathogenic calls
    among them, which add a not-a-diagnosis warning.
    """
    result = current_result()
    metrics = summary.metrics()
    if output is not None:
        result.add_output(output, role, metrics=metrics)
    if table is not None:
        result.add_output(table, "confidence_table", kind="tsv")
    totals: dict[str, Scalar] = {}
    for key, value in metrics.items():
        previous = result.metrics.get(key)
        if isinstance(value, int) and isinstance(previous, int):
            value += previous
        totals[key] = value
    result.add_metrics(**totals)
    details = dict(result.details or {})
    details["confidence"] = [*details.get("confidence", []), summary.details(
        None if output is None else str(output), role
    )]  # fmt: skip
    result.set_details(details)

    if not summary.carried:
        logging.info(f"No {what} are carried by the sample.")
        return
    logging.info(
        f"Confidence of {summary.carried} {what}: {_counts_text(summary)} "
        f"(caller: {summary.caller}; {GUIDE})."
    )
    where = f" (see {table})" if table else ""
    low = summary.counts[LOW]
    if low:
        top = ", ".join(
            code
            for code, _ in Counter(
                {k: v for k, v in summary.reasons.items() if k in LOW_REASONS}
            ).most_common(3)
        )
        warn(
            "low_confidence_findings",
            f"{low} of {summary.carried} {what} are low confidence ({top}){where}: "
            "the reads don't support them well enough to trust without "
            "confirmation.",
            count=low,
            total=summary.carried,
            output=None if output is None else str(output),
            reasons=top,
        )
    if clinical:
        warn(
            "unconfirmed_clinical_findings",
            f"ClinVar lists {clinical} of the sample's calls as pathogenic or "
            f"likely pathogenic{where}. {DISCLAIMER}",
            count=clinical,
            output=None if output is None else str(output),
        )


def is_pathogenic(clnsig: str) -> bool:
    """Whether a ClinVar CLNSIG value includes pathogenic or likely pathogenic."""
    return "Pathogenic" in clnsig or "Likely_pathogenic" in clnsig


def clinical_count(summary: ConfidenceSummary) -> int:
    return sum(is_pathogenic(f.annotations.get("CLNSIG", "")) for f in summary.findings)
