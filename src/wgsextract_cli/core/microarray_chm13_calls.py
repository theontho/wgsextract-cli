"""Strict native SNP calls projected onto existing GRCh37/rCRS templates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import NamedTuple

from wgsextract_cli.core.microarray_identity import MicroarrayIdentity
from wgsextract_cli.core.microarray_quality import MicroarrayQuality
from wgsextract_cli.core.microarray_site_map import SiteMap, SiteProjection
from wgsextract_cli.core.utils import WGSExtractError

_COMPLEMENT = str.maketrans("ACGT", "TGCA")


class NativeCall(NamedTuple):
    genotype: str
    reason: str


class ProjectedCall(NamedTuple):
    site: SiteProjection
    snp_id: str
    genotype: str
    reason: str


def canonical_chromosome(chrom: str) -> str:
    suffix = chrom.removeprefix("chr")
    return "chrM" if suffix in {"M", "MT"} else f"chr{suffix}"


def _record_call(
    ref: str,
    alt: str,
    genotype: str,
    dp: str,
    gq: str,
    record_filter: str,
    quality: MicroarrayQuality,
) -> NativeCall:
    alleles = [ref, *([] if alt == "." else alt.split(","))]
    if any(len(allele) != 1 for allele in alleles) or {"I", "D"} & set(alleles):
        return NativeCall("--", "unsupported_indel")
    if any(allele not in "ACGT" for allele in alleles):
        return NativeCall("--", "unsupported_allele")
    indices = re.split(r"[/|]", genotype)
    if "." in indices:
        return NativeCall("--", "missing_gt")
    if not 1 <= len(indices) <= 2 or any(not index.isdecimal() for index in indices):
        return NativeCall("--", "unsupported_gt")
    numbers = [int(index) for index in indices]
    if any(index >= len(alleles) for index in numbers):
        return NativeCall("--", "unsupported_gt")
    rejection = quality.rejection(dp, gq, record_filter)
    if rejection:
        return NativeCall("--", rejection)
    bases = [alleles[index] for index in numbers]
    return NativeCall(
        "".join(sorted(bases if len(bases) == 2 else bases * 2)), "called"
    )


def parse_native_calls(
    lines: Iterable[str],
    mapping: SiteMap,
    marker_ids: Mapping[tuple[str, int], str],
    quality: MicroarrayQuality,
    identity: MicroarrayIdentity,
) -> tuple[dict[tuple[str, int], NativeCall], set[str]]:
    """Consume CHROM/POS/END/REF/ALT/GT/DP/GQ/FILTER for one sample."""
    calls: dict[tuple[str, int], NativeCall] = {}
    represented: set[str] = set()
    for line in lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 9:
            raise WGSExtractError("CHM13 microarray requires exactly one VCF sample.")
        chrom, pos_text, end_text, ref, alt, gt, dp, gq, record_filter = fields
        chrom = canonical_chromosome(chrom)
        try:
            pos, end = int(pos_text), int(end_text)
            if not 1 <= pos <= end <= 0xFFFFFFFF:
                raise ValueError("invalid interval")
        except ValueError as error:
            raise WGSExtractError("Invalid native VCF position or END.") from error
        represented.add(chrom)
        call = _record_call(
            ref.upper(), alt.upper(), gt, dp, gq, record_filter, quality
        )
        if end != pos and call.reason != "unsupported_indel":
            call = NativeCall("--", "unsupported_allele")
        for site in mapping.native_range(chrom, pos, end):
            key = (site.source_chrom, site.source_pos)
            snp_id = marker_ids.get(key)
            if snp_id is None:
                raise WGSExtractError(
                    "CHM13 map and installed template markers disagree."
                )
            rejection = identity.rejection(snp_id, ref, alt)
            current = NativeCall("--", rejection) if rejection else call
            previous = calls.get(key)
            if previous is not None:
                if previous.reason != "called":
                    continue
                if current.reason == "called" and current.genotype != previous.genotype:
                    current = NativeCall("--", "conflicting_duplicate")
            calls[key] = current
    return calls, represented


def project_native_calls(
    sites: Iterable[SiteProjection],
    marker_ids: Mapping[tuple[str, int], str],
    calls: Mapping[tuple[str, int], NativeCall],
    *,
    quality: MicroarrayQuality,
    identity: MicroarrayIdentity,
    is_vcf: bool,
    represented: set[str],
    native_bases: Mapping[tuple[str, int], str],
    marker_rejections: Mapping[tuple[str, int], str] | None = None,
    rejected_ids: set[str] | None = None,
    read_evidence: Mapping[tuple[str, int], str] | None = None,
) -> Iterator[ProjectedCall]:
    marker_rejections = marker_rejections or {}
    rejected_ids = rejected_ids or set()
    read_evidence = read_evidence or {}
    for site in sites:
        key = (site.source_chrom, site.source_pos)
        snp_id = marker_ids.get(key)
        if snp_id is None:
            raise WGSExtractError("CHM13 map and installed template markers disagree.")
        rejection = marker_rejections.get(key)
        if rejection or rejected_ids and snp_id in rejected_ids:
            yield ProjectedCall(
                site, snp_id, "--", rejection or "unsupported_indel_marker"
            )
            continue
        if site.status != "mapped":
            yield ProjectedCall(site, snp_id, "--", site.status)
            continue
        assert site.target_chrom is not None and site.target_pos is not None
        call = calls.get(key)
        if call is None:
            if not is_vcf:
                call = NativeCall(
                    "--",
                    read_evidence.get((site.target_chrom, site.target_pos), "no_reads"),
                )
            elif site.target_chrom not in represented:
                call = NativeCall("--", "absent_contig")
            elif not quality.assume_hom_ref:
                call = NativeCall("--", "absent_from_vcf")
            elif quality.min_dp or quality.min_gq:
                call = NativeCall(
                    "--", quality.rejection(".", ".", "PASS") or "missing_gt"
                )
            else:
                assert site.target_chrom is not None and site.target_pos is not None
                base = native_bases.get((site.target_chrom, site.target_pos), "")
                call = (
                    NativeCall(base * 2, "assumed_hom_ref")
                    if base in {"A", "C", "G", "T"}
                    else NativeCall("--", "ambiguous_reference")
                )
        rejection = identity.rejection(snp_id)
        genotype, reason = ("--", rejection) if rejection else call
        if site.inverted and genotype != "--":
            genotype = "".join(sorted(genotype.translate(_COMPLEMENT)))
        yield ProjectedCall(site, snp_id, genotype, reason)
