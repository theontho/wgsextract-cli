"""Manifest-defined CHM13 I/D calls, independently evidenced and vendor-only."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from wgsextract_cli.core.microarray_chm13 import CHM13Catalogue
from wgsextract_cli.core.microarray_chm13_calls import canonical_chromosome
from wgsextract_cli.core.microarray_identity import identity_policy
from wgsextract_cli.core.microarray_indel_probes import (
    IndelProbe,
    IndelSite,
    match_indel_reference,
    read_indel_manifest,
)
from wgsextract_cli.core.microarray_indels import (
    IndelCalls,
    _calling_regions,
    _normalize_sites,
    _query_lines,
)
from wgsextract_cli.core.microarray_quality import quality_policy
from wgsextract_cli.core.microarray_utils import (
    _resolve_templates_for_format,
    get_template_format,
    template_rows,
    vendor_chrom,
    vendor_output_profile,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline

_FLANK = 15
_MAX_REPEAT_SHIFT = 4096
_MIN_SUPPORT = 2
_MAPQ = 20
_BASEQ = 20
_CIGAR = re.compile(r"(\d+)([MIDNSHP=X])")


@dataclass(frozen=True)
class FlankedSite:
    site: IndelSite
    left: str
    right: str

    @property
    def start(self) -> int:
        return self.site.position - len(self.left)

    @property
    def end(self) -> int:
        return self.site.position + len(self.site.ref) - 1 + len(self.right)


def _sequences(fasta: str, regions: Iterable[str], directory: Path) -> dict[str, str]:
    path = directory / "reference-windows.txt"
    path.write_text("".join(region + "\n" for region in regions), newline="\n")
    result: dict[str, str] = {}
    current = ""
    for line in _query_lines(
        ["samtools", "faidx", "-r", str(path), fasta], str(directory)
    ):
        if line.startswith(">"):
            current = line[1:]
            result[current] = ""
        elif current:
            result[current] += line.upper()
    return result


def native_probe_sites(
    probes: Mapping[str, IndelProbe],
    catalogue: CHM13Catalogue,
    names: Mapping[str, str],
    fasta: str,
    directory: Path,
) -> dict[str, IndelSite]:
    """Use reciprocal mapping only to locate; both complete DNA flanks certify."""
    windows: dict[str, list[str]] = defaultdict(list)
    for key, probe in probes.items():
        mapped = catalogue.mapping.source_lookup(
            canonical_chromosome(probe.chrom), probe.position
        )
        if mapped is None or mapped.status != "mapped":
            continue
        assert mapped.target_chrom is not None and mapped.target_pos is not None
        radius = len(probe.context) + 100
        first = max(1, mapped.target_pos - radius)
        last = min(
            catalogue.contigs[mapped.target_chrom].length, mapped.target_pos + radius
        )
        windows[f"{names[mapped.target_chrom]}:{first}-{last}"].append(key)
    if not windows:
        return {}
    sequences = _sequences(fasta, windows, directory)
    matched = {}
    for region, keys in windows.items():
        chrom, span = region.rsplit(":", 1)
        first = int(span.split("-")[0])
        for key in keys:
            site = match_indel_reference(probes[key], chrom, first, sequences[region])
            if site is not None:
                matched[key] = site
    if not matched:
        return {}
    normalized = _normalize_sites(matched, fasta, str(directory))
    # Include definitions rejected at identical anchors when finding *other*
    # overlapping events; dropping one collision partner must not rescue another.
    all_sites = []
    with (directory / "normalized-probe-sites.vcf").open() as stream:
        for line in stream:
            if not line.startswith("#"):
                chrom, pos, key, ref, alt = line.split("\t")[:5]
                all_sites.append(
                    (
                        key,
                        replace(
                            matched[key],
                            chrom=chrom,
                            position=int(pos),
                            ref=ref,
                            alt=alt,
                        ),
                    )
                )
    rejected: set[str] = set()
    active: list[tuple[str, IndelSite]] = []
    for key, site in sorted(all_sites, key=lambda row: (row[1].chrom, row[1].position)):
        active = [
            row
            for row in active
            if row[1].chrom == site.chrom
            and row[1].position + len(row[1].ref) - 1 >= site.position
        ]
        for previous_key, previous in active:
            if previous != site:
                rejected.update((key, previous_key))
        active.append((key, site))
    return {key: site for key, site in normalized.items() if key not in rejected}


def flanked_sites(
    sites: Mapping[str, IndelSite], fasta: str, directory: Path
) -> dict[str, FlankedSite]:
    regions = {
        key: f"{site.chrom}:{site.position - _FLANK}-{site.position + len(site.ref) - 1 + _MAX_REPEAT_SHIFT + _FLANK}"
        for key, site in sites.items()
        if site.position > _FLANK
    }
    if not regions:
        return {}
    sequences = _sequences(fasta, dict.fromkeys(regions.values()), directory)
    result = {}
    for key, region in regions.items():
        site, sequence = sites[key], sequences[region]
        if (
            min(len(site.ref), len(site.alt)) != 1
            or len(site.ref) == len(site.alt)
            or site.ref[0] != site.alt[0]
            or sequence[_FLANK : _FLANK + len(site.ref)] != site.ref
        ):
            continue
        motif = (site.ref if len(site.ref) > len(site.alt) else site.alt)[1:]
        if set(motif) - set("ACGT"):
            continue
        offset = _FLANK + len(site.ref)
        shift = 0
        while (
            shift < _MAX_REPEAT_SHIFT
            and offset + shift < len(sequence)
            and sequence[offset + shift] == motif[shift % len(motif)]
        ):
            shift += 1
        # A missing/unknown next base cannot certify the rightmost placement.
        if (
            shift == _MAX_REPEAT_SHIFT
            or offset + shift >= len(sequence)
            or sequence[offset + shift] not in "ACGT"
        ):
            continue
        end = offset + shift + _FLANK
        context = sequence[:end]
        if len(context) != end or set(context) - set("ACGT"):
            continue
        left, right = context[:_FLANK], context[offset:]
        alternate = left + site.alt + right
        if any(
            text.find(anchor) < 0 or text.find(anchor) != text.rfind(anchor)
            for anchor in (left, right[-_FLANK:])
            for text in (sequence, alternate)
        ):
            continue
        result[key] = FlankedSite(site, left, right)
    return result


def expected_ploidy(path: Path, sex: str, site: FlankedSite) -> int:
    """No call spans a PAR boundary, a masked Y interval or the circular origin."""
    rows = [
        line.split()
        for line in path.read_text().splitlines()
        if not line.startswith("#")
    ]
    relevant = [
        row
        for row in rows
        if len(row) == 5
        and row[0] == site.site.chrom
        and row[3] == sex
        and int(row[1]) <= site.end
        and int(row[2]) >= site.start
    ]
    covering = [
        int(row[4])
        for row in relevant
        if int(row[1]) <= site.start and int(row[2]) >= site.end
    ]
    return covering[0] if len(covering) == len(relevant) == 1 else 0


def read_haplotype(fields: list[str], probe: FlankedSite) -> str | None:
    """Span all repeat-equivalent placements and both unique outer flanks.

    An overlapping deletion/skip that removes a flank is ambiguous (``!``),
    not evidence for the shorter probe allele. Unmatched/low-quality reads
    cannot support a reference call.
    """
    cigar = _CIGAR.findall(fields[5])
    if not cigar or "".join(n + op for n, op in cigar) != fields[5]:
        return None
    sequence, qualities = fields[9:11]
    if sequence == "*" or qualities == "*" or len(sequence) != len(qualities):
        return None
    ref, query = int(fields[3]), 0
    initial = ref
    pieces = []
    ambiguous = False
    flank_deletion = False
    core_first = probe.site.position
    core_last = probe.end - _FLANK
    for size, operation in cigar:
        length = int(size)
        if operation in "M=X":
            first, last = max(ref, probe.start), min(ref + length - 1, probe.end)
            if first <= last:
                a, b = query + first - ref, query + last - ref + 1
                if b > len(sequence) or any(
                    ord(q) - 33 < _BASEQ for q in qualities[a:b]
                ):
                    return None
                pieces.append(sequence[a:b])
            ref += length
            query += length
        elif operation == "I":
            if probe.start <= ref - 1 < probe.end:
                if query + length > len(sequence) or any(
                    ord(q) - 33 < _BASEQ for q in qualities[query : query + length]
                ):
                    return None
                pieces.append(sequence[query : query + length])
            query += length
        elif operation in "DN":
            last = ref + length - 1
            if ref <= core_last and last >= core_first:
                if operation == "N" or ref < probe.start or last > probe.end:
                    ambiguous = True
                elif ref < core_first or last > core_last:
                    flank_deletion = True
            ref += length
        elif operation == "S":
            query += length
    if ambiguous:
        return "!"
    if initial > probe.start or ref - 1 < probe.end:
        return "!" if flank_deletion else None
    haplotype = "".join(pieces).upper()
    if not haplotype.startswith(probe.left) or not haplotype.endswith(probe.right):
        return "!" if flank_deletion else None
    if haplotype == probe.left + probe.site.ref + probe.right:
        return "ref"
    if haplotype == probe.left + probe.site.alt + probe.right:
        return "alt"
    return "!"


def flank_evidence(
    lines: Iterable[str], sites: Mapping[str, FlankedSite]
) -> dict[str, Counter[str]]:
    by_chrom: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for key, probe in sites.items():
        by_chrom[probe.site.chrom].append((probe.site.position, key))
    positions = {}
    longest = {}
    for chrom, rows in by_chrom.items():
        rows.sort()
        positions[chrom] = [row[0] for row in rows]
        longest[chrom] = max(len(sites[key].site.ref) for _, key in rows)
    fragments: dict[str, dict[str, str]] = defaultdict(dict)
    quarantined = set()
    for line in lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 11 or fields[2] not in by_chrom:
            continue
        if int(fields[1]) & 3844 or int(fields[4]) < _MAPQ:
            continue
        start = int(fields[3])
        span = sum(int(n) for n, op in _CIGAR.findall(fields[5]) if op in "MDN=X")
        end = start + span - 1
        chrom = fields[2]
        for _, key in by_chrom[chrom][
            bisect.bisect_left(
                positions[chrom], start - longest[chrom] + 1
            ) : bisect.bisect_right(positions[chrom], end)
        ]:
            if sites[key].site.position + len(sites[key].site.ref) - 1 < start:
                continue
            outcome = read_haplotype(fields, sites[key])
            if outcome is None:
                continue
            prior = fragments[key].get(fields[0])
            if outcome == "!" or prior is not None and prior != outcome:
                quarantined.add(key)
            if len(fragments[key]) < 1000:
                fragments[key][fields[0]] = outcome
            else:
                quarantined.add(key)
    return {
        key: Counter(fragments[key].values())
        + Counter({"ambiguous": int(key in quarantined)})
        for key in sites
    }


def supported_call(
    genotype: str, site: IndelSite, evidence: Counter[str], ploidy: int
) -> str:
    if not ploidy or len(genotype) != ploidy or set(genotype) - {"I", "D"}:
        return "--"
    if evidence["ambiguous"]:
        return "--"
    expected = {"ref" if code == site.ref_code else "alt" for code in genotype}
    if any(evidence[allele] < _MIN_SUPPORT for allele in expected):
        return "--"
    if any(evidence[allele] for allele in {"ref", "alt"} - expected):
        return "--"
    return genotype


def likelihood_gq(gt: str, values: str) -> str:
    """bcftools can omit GQ for forced 0/0 calls; require complete biallelic PL."""
    tokens = re.split(r"[/|]", gt)
    if not 1 <= len(tokens) <= 2 or any(token not in {"0", "1"} for token in tokens):
        return "."
    parts = values.split(",")
    if len(parts) != len(tokens) + 1 or any(not part.isdecimal() for part in parts):
        return "."
    likelihoods = [int(part) for part in parts]
    chosen = sum(map(int, tokens))
    best_other = min(
        value for index, value in enumerate(likelihoods) if index != chosen
    )
    return str(max(0, best_other - likelihoods[chosen]))


def _native_calls(
    vcf: str,
    sites: dict[str, IndelSite],
    args: argparse.Namespace,
    directory: Path,
    marker_ids: dict[str, str],
    ploidies: dict[str, int],
) -> dict[str, str]:
    from wgsextract_cli.core.microarray_indels import _calls_from_vcf

    quality = quality_policy(args)
    query_args = argparse.Namespace(**vars(args))
    query_args.min_dp, query_args.min_gq = max(4, quality.min_dp), 0
    result = _calls_from_vcf(vcf, sites, query_args, str(directory), marker_ids)
    policy = replace(
        quality, min_dp=max(4, quality.min_dp), min_gq=max(20, quality.min_gq)
    )
    by_position: dict[tuple[str, int], list[str]] = defaultdict(list)
    for key, site in sites.items():
        by_position[site.chrom, site.position].append(key)
    query = "%CHROM\t%POS\t%REF\t%ALT\t%FILTER[\t%GT\t%DP\t%GQ\t%PL]\n"
    for line in _query_lines(
        ["bcftools", "query", "-u", "-f", query, vcf], str(directory)
    ):
        chrom, pos, ref, alt, filtered, gt, dp, gq, pl = line.split("\t")
        for key in by_position.get((chrom, int(pos)), []):
            if (
                ref != sites[key].ref
                or alt != sites[key].alt
                or len(re.split(r"[/|]", gt)) != ploidies[key]
                or policy.rejection(
                    dp, likelihood_gq(gt, pl) if gq == "." else gq, filtered
                )
            ):
                result[key] = "--"
    return result


def call_chm13_indels(
    bindings: dict[str, str],
    roots: list[str],
    *,
    args: argparse.Namespace,
    catalogue: CHM13Catalogue,
    names: Mapping[str, str],
    fasta: str,
    directory: Path,
    regions: list[tuple[str, int, int]],
    ploidy_path: Path,
    sex_args: list[str],
    threads: str,
) -> IndelCalls:
    """Overlay only a bound vendor's exact probe rows; never modify SNP calls."""
    from wgsextract_cli.commands._microarray_combined import _command_lines

    directory.mkdir()
    probes: dict[str, IndelProbe] = {}
    metadata = {}
    output: IndelCalls = {}
    hashes = {}
    counts: Counter[str] = Counter()
    for format_name, manifest in bindings.items():
        with open(manifest, "rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            hashes[format_name] = digest.hexdigest()
        try:
            definitions = read_indel_manifest(manifest)
        except WGSExtractError as error:
            if str(error) != "No unambiguous sequence-defined I/D probes in manifest.":
                raise
            definitions = {}
        info = get_template_format(format_name)
        if info is None or vendor_output_profile(format_name).indel_encoding != "ID":
            raise WGSExtractError(f"No verified I/D output encoding for {format_name}.")
        _, bodies = _resolve_templates_for_format(roots, format_name, info)
        output[format_name] = {}
        if info.get("ncbi36"):
            continue
        for body in bodies:
            for marker, chrom, pos in template_rows(format_name, body):
                if not pos.isdecimal():
                    continue
                probe = definitions.get((marker, vendor_chrom(chrom), int(pos)))
                if probe is None:
                    continue
                key = "P" + str(len(probes))
                probes[key] = probe
                metadata[key] = format_name, marker, chrom, pos
                output[format_name][marker, chrom, pos] = "--"
    counts["matched_probe_definitions"] = len(probes)
    sites = native_probe_sites(probes, catalogue, names, fasta, directory)
    sites = {
        key: site
        for key, site in sites.items()
        if not regions
        or any(
            canonical_chromosome(site.chrom) == chrom and start <= site.position <= end
            for chrom, start, end in regions
        )
    }
    flanked = flanked_sites(sites, fasta, directory)
    sex = "M" if sex_args else "F"
    ploidies = {
        key: expected_ploidy(ploidy_path, sex, probe) for key, probe in flanked.items()
    }
    sites = {key: site for key, site in sites.items() if ploidies.get(key)}
    flanked = {key: probe for key, probe in flanked.items() if key in sites}
    counts["verified_normalized_callable_probes"] = len(sites)
    calls = {}
    evidence: dict[str, Counter[str]] = {}
    if sites:
        targets = directory / "alleles.tsv"
        targets.write_text(
            "".join(
                f"{site.chrom}\t{site.position}\t{site.ref},{site.alt}\n"
                for site in sorted(
                    set(sites.values()), key=lambda site: (site.chrom, site.position)
                )
            ),
            newline="\n",
        )
        run_command(["bgzip", "-f", str(targets)])
        target = str(targets) + ".gz"
        run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", target])
        intervals = directory / "calling-regions.tsv"
        _calling_regions(sites, intervals, padding=_FLANK)
        vcf = str(directory / "calls.vcf.gz")
        run_pipeline(
            [
                [
                    "bcftools",
                    "mpileup",
                    "-Ou",
                    "-B",
                    "-C",
                    "50",
                    "-q",
                    str(_MAPQ),
                    "-Q",
                    str(_BASEQ),
                    "-a",
                    "FORMAT/DP,FORMAT/AD",
                    "--threads",
                    threads,
                    "-f",
                    fasta,
                    "-T",
                    target,
                    "-R",
                    str(intervals),
                    args.input,
                ],
                [
                    "bcftools",
                    "call",
                    "-m",
                    "-C",
                    "alleles",
                    "-T",
                    target,
                    "--keep-alts",
                    "-f",
                    "GQ",
                    "--ploidy-file",
                    str(ploidy_path),
                    *sex_args,
                    "-Ou",
                ],
                ["bcftools", "norm", "--check-ref", "e", "-f", fasta, "-Oz", "-o", vcf],
            ],
            stdout=None,
            description="Call complete-flank CHM13 I/D probes",
        )
        calls = _native_calls(
            vcf,
            sites,
            args,
            directory,
            {key: data[1] for key, data in metadata.items()},
            ploidies,
        )
        bed = directory / "read-windows.bed"
        bed.write_text(
            "".join(
                f"{probe.site.chrom}\t{probe.start - 1}\t{probe.end}\n"
                for probe in flanked.values()
            ),
            newline="\n",
        )
        evidence = flank_evidence(
            _command_lines(
                [
                    "samtools",
                    "view",
                    "-M",
                    "-L",
                    str(bed),
                    "-q",
                    str(_MAPQ),
                    "-F",
                    "3844",
                    "-T",
                    fasta,
                    args.input,
                ],
                str(directory),
            ),
            flanked,
        )
    with (directory / "probe-calls.tsv").open("w", newline="\n") as stream:
        stream.write(
            "#PROBE\tFORMAT\tID\tCHROM\tPOS\tNATIVE_CHROM\tNATIVE_POS\tREF\tALT\tREF_CODE\tPLOIDY\tRESULT\tREF_FRAGMENTS\tALT_FRAGMENTS\tAMBIGUOUS\n"
        )
        for key, data in metadata.items():
            site = sites.get(key)
            support = evidence.get(key, Counter())
            call = (
                supported_call(
                    calls.get(key, "--"), site, support, ploidies.get(key, 0)
                )
                if site
                else "--"
            )
            if identity_policy(args).rejection(data[1]):
                call = "--"
            counts["called" if call != "--" else "no_call"] += 1
            format_name, marker, chrom, pos = data
            if (
                len(call) == 1
                and vendor_output_profile(format_name).haploid_style != "single"
            ):
                call *= 2
            output[format_name][marker, chrom, pos] = call
            stream.write(
                "\t".join(
                    map(
                        str,
                        (
                            key,
                            *data,
                            site.chrom if site else ".",
                            site.position if site else ".",
                            site.ref if site else ".",
                            site.alt if site else ".",
                            site.ref_code if site else ".",
                            ploidies.get(key, 0),
                            call,
                            support["ref"],
                            support["alt"],
                            support["ambiguous"],
                        ),
                    )
                )
                + "\n"
            )
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "counts": dict(counts),
                "manifest_sha256_by_format": hashes,
                "minimum_flank_bases": _FLANK,
                "maximum_repeat_equivalent_shift": _MAX_REPEAT_SHIFT,
                "unique_outer_anchors_required": True,
                "minimum_fragments_per_called_allele": _MIN_SUPPORT,
                "minimum_mapping_quality": _MAPQ,
                "minimum_base_quality": _BASEQ,
                "minimum_dp": max(4, quality_policy(args).min_dp),
                "minimum_gq": max(20, quality_policy(args).min_gq),
                "reference_filling": False,
                "sex": sex,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        newline="\n",
    )
    return output
