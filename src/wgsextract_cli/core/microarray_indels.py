"""Opt-in, manifest-defined indel calling, isolated from the legacy SNP stream."""

import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from wgsextract_cli.core.builds import canonical_build
from wgsextract_cli.core.microarray_aadr import aadr_lifter
from wgsextract_cli.core.microarray_identity import identity_policy
from wgsextract_cli.core.microarray_indel_probes import (
    IndelProbe,
    IndelSite,
    match_indel_reference,
    read_indel_manifest,
)
from wgsextract_cli.core.microarray_quality import quality_policy
from wgsextract_cli.core.microarray_utils import (
    _haploid_letter,
    _resolve_templates_for_format,
    get_template_format,
    template_rows,
    vendor_chrom,
    vendor_output_profile,
)
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import (
    match_chromosome_name,
    vcf_index_chromosomes,
)

IndelCalls = dict[str, dict[tuple[str, str, str], str]]


def indel_manifest_bindings(
    args: argparse.Namespace, resolve_format: Callable[[str], str | None]
) -> dict[str, str]:
    bindings = {}
    selected = {resolve_format(value) for value in args.formats.split(",")}
    for entry in getattr(args, "indel_manifest", []):
        name, separator, path = entry.partition("=")
        name = resolve_format(name)
        if not separator or not name or not path:
            raise WGSExtractError("--indel-manifest requires FORMAT=PATH.")
        if name not in selected:
            raise WGSExtractError(f"Indel format {name} must be selected in --formats.")
        if (
            not get_template_format(name)
            or vendor_output_profile(name).indel_encoding != "ID"
        ):
            raise WGSExtractError(f"No verified I/D output encoding for {name}.")
        if name in bindings:
            raise WGSExtractError(f"Duplicate indel manifest for {name}.")
        if not Path(path).is_file():
            raise WGSExtractError(f"Indel manifest not found: {path}")
        bindings[name] = path
    return bindings


def _write_summary(
    workdir: str, counts: Counter[str], hashes: dict[str, str], build: str | None
) -> None:
    Path(workdir, "summary.json").write_text(
        json.dumps(
            {
                "counts": dict(counts),
                "manifest_sha256_by_format": hashes,
                "reference_build": build,
            },
            indent=2,
        )
        + "\n"
    )


def _query_lines(command: list[str], workdir: str) -> list[str]:
    with tempfile.TemporaryFile("w+", dir=workdir) as stream:
        run_command(command, stdout=stream)
        stream.seek(0)
        return stream.read().splitlines()


def _calling_regions(
    sites: dict[str, IndelSite], path: Path, *, padding: int = 0
) -> None:
    merged: list[tuple[str, int, int]] = []
    for site in sorted(set(sites.values()), key=lambda s: (s.chrom, s.position)):
        start = max(1, site.position - padding)
        end = site.position + len(site.ref) - 1 + padding
        if merged and merged[-1][0] == site.chrom and start <= merged[-1][2] + 1:
            chrom, start, previous_end = merged[-1]
            merged[-1] = chrom, start, max(previous_end, end)
        else:
            merged.append((site.chrom, start, end))
    path.write_text("".join(f"{c}\t{s}\t{e}\n" for c, s, e in merged))


def _reference_windows(
    probes: dict[str, IndelProbe],
    ref_fasta: str,
    build: str,
    root: str | None,
    workdir: str,
) -> dict[str, IndelSite]:
    if not Path(ref_fasta + ".fai").exists():
        run_command(["samtools", "faidx", ref_fasta])
    contigs = {}
    with open(ref_fasta + ".fai") as stream:
        for line in stream:
            parts = line.split("\t")
            contigs[parts[0]] = int(parts[1])
    lifter = aadr_lifter(build, workdir, root)
    regions: dict[str, list[str]] = defaultdict(list)
    for key, probe in probes.items():
        chrom, position = probe.chrom, probe.position
        if lifter:
            hits = lifter.convert_coordinate("chr" + chrom, position - 1)
            if not hits or len(hits) != 1 or "_" in hits[0][0]:
                continue
            chrom, position = hits[0][0], int(hits[0][1]) + 1
        matched = match_chromosome_name(chrom, set(contigs))
        if not matched:
            continue
        radius = len(probe.context) + 100
        start, end = max(1, position - radius), min(contigs[matched], position + radius)
        regions[f"{matched}:{start}-{end}"].append(key)
    regions_path = str(Path(workdir, "probe-windows.txt"))
    Path(regions_path).write_text("".join(region + "\n" for region in regions))
    lines = _query_lines(["samtools", "faidx", "-r", regions_path, ref_fasta], workdir)
    sequences: dict[str, str] = {}
    current = ""
    for line in lines:
        if line.startswith(">"):
            current = line[1:]
            sequences[current] = ""
        elif current:
            sequences[current] += line
    sites = {}
    for region, keys in regions.items():
        chrom, interval = region.rsplit(":", 1)
        start = int(interval.split("-")[0])
        for key in keys:
            site = match_indel_reference(
                probes[key], chrom, start, sequences.get(region, "")
            )
            if site:
                sites[key] = site
    return sites


def _normalize_sites(
    sites: dict[str, IndelSite], ref_fasta: str, workdir: str
) -> dict[str, IndelSite]:
    raw = Path(workdir, "probe-sites.vcf")
    with raw.open("w", newline="\n") as stream:
        stream.write("##fileformat=VCFv4.2\n")
        for chrom in sorted({s.chrom for s in sites.values()}):
            stream.write(f"##contig=<ID={chrom}>\n")
        stream.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
        for key, site in sites.items():
            stream.write(
                f"{site.chrom}\t{site.position}\t{key}\t{site.ref}\t{site.alt}\t.\t.\t.\n"
            )
    normalized = str(Path(workdir, "normalized-probe-sites.vcf"))
    run_command(
        ["bcftools", "norm", "-f", ref_fasta, "-Ov", "-o", normalized, str(raw)]
    )
    result = {}
    with open(normalized) as stream:
        for line in stream:
            if line.startswith("#"):
                continue
            chrom, position, key, ref, alt = line.split("\t")[:5]
            result[key] = replace(
                sites[key], chrom=chrom, position=int(position), ref=ref, alt=alt
            )
    alleles: dict[tuple[str, int], set[tuple[str, str]]] = defaultdict(set)
    for site in result.values():
        alleles[site.chrom, site.position].add((site.ref, site.alt))
    return {
        key: site
        for key, site in result.items()
        if len(alleles[site.chrom, site.position]) == 1
    }


def _calls_from_vcf(
    vcf: str,
    sites: dict[str, IndelSite],
    args: argparse.Namespace,
    workdir: str,
    marker_ids: dict[str, str] | None = None,
) -> dict[str, str]:
    samples = _query_lines(["bcftools", "query", "-l", vcf], workdir)
    if len(samples) != 1:
        raise WGSExtractError("Indel calling requires a single-sample VCF.")
    policy = quality_policy(args)
    identity = identity_policy(args)
    by_position: dict[tuple[str, int], list[str]] = defaultdict(list)
    for key, site in sites.items():
        by_position[site.chrom, site.position].append(key)
    calls: dict[str, str] = {}
    rejected: set[str] = set()
    query = "%CHROM\t%POS\t%ID\t%REF\t%ALT\t%FILTER[\t%GT\t%DP\t%GQ]\n"
    for line in _query_lines(["bcftools", "query", "-u", "-f", query, vcf], workdir):
        chrom, pos, source_id, ref, alt, filtered, gt, dp, gq = line.split("\t")
        keys = by_position.get((chrom, int(pos)), [])
        for key in keys:
            site = sites[key]
            alleles = [ref, *alt.split(",")]
            tokens = gt.replace("|", "/").split("/")
            codes = {site.ref: site.ref_code, site.alt: site.alt_code}
            result = "--"
            identity_rejected = identity.rejection(
                (marker_ids or {}).get(key, "."), site.ref, site.alt
            ) or any(
                identity.rejection(rsid, ref, alt) for rsid in source_id.split(";")
            )
            if (
                ref.upper() == site.ref
                and site.alt in {a.upper() for a in alt.split(",")}
                and dp != "0"
                and not policy.rejection(dp, gq, filtered)
                and not identity_rejected
                and 1 <= len(tokens) <= 2
            ):
                if all(t.isdigit() and int(t) < len(alleles) for t in tokens):
                    genotype = [alleles[int(t)].upper() for t in tokens]
                    if all(a in codes for a in genotype):
                        result = "".join(sorted(codes[a] for a in genotype))
            if key in rejected:
                continue
            if result == "--" or (key in calls and calls[key] != result):
                rejected.add(key)
                calls[key] = "--"
            else:
                calls[key] = result
    return calls


def call_manifest_indels(
    bindings: dict[str, str],
    template_roots: list[str],
    *,
    args: argparse.Namespace,
    lib: ReferenceLibrary,
    outdir: str,
    ploidy_args: list[str],
    threads: str,
) -> IndelCalls:
    if not lib.fasta:
        raise WGSExtractError("Indel probes require a reference FASTA.")
    if canonical_build(lib.build or "") not in {"hg19", "hg38"}:
        raise WGSExtractError(
            "Indel manifests require a known GRCh37 or GRCh38 reference."
        )
    workdir = tempfile.mkdtemp(prefix="Indels_", dir=outdir)
    probes: dict[str, IndelProbe] = {}
    metadata: dict[str, tuple[str, str, str, str]] = {}
    output: IndelCalls = {}
    manifest_hashes = {}
    counts: Counter[str] = Counter()
    for format_name, manifest in bindings.items():
        if vendor_output_profile(format_name).indel_encoding != "ID":
            raise WGSExtractError(f"No verified I/D output encoding for {format_name}.")
        fmt = get_template_format(format_name)
        if not fmt:
            raise WGSExtractError(f"Unknown indel output format: {format_name}")
        defined_ids: set[str] = set()
        definitions = read_indel_manifest(manifest, indel_ids=defined_ids)
        with open(manifest, "rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            manifest_hashes[format_name] = digest.hexdigest()
        _, paths = _resolve_templates_for_format(template_roots, format_name, fmt)
        output[format_name] = {}
        for path in paths:
            for marker, chrom, position in template_rows(format_name, path):
                if not position.isdigit():
                    continue
                if marker in defined_ids:
                    output[format_name][marker, chrom, position] = "--"
                probe = definitions.get((marker, vendor_chrom(chrom), int(position)))
                if probe:
                    key = "P" + str(len(probes))
                    probes[key] = probe
                    metadata[key] = format_name, marker, chrom, position
                    output[format_name][marker, chrom, position] = "--"
    counts["matched_probe_definitions"] = len(probes)
    sites = _reference_windows(probes, lib.fasta, lib.build or "", lib.root, workdir)
    if sites:
        sites = _normalize_sites(sites, lib.fasta, workdir)
    if not sites:
        raise WGSExtractError(
            "No manifest probes match the selected reference unambiguously."
        )
    if args.region:
        intervals = []
        for region in args.region.split(","):
            match = re.fullmatch(r"([^:]+)(?::(\d+)(?:-(\d+))?)?", region)
            if not match:
                raise WGSExtractError("Indel regions must be CHROM or CHROM:START-END.")
            name, first, last = match.groups()
            chrom_name = match_chromosome_name(name, {s.chrom for s in sites.values()})
            intervals.append((chrom_name, int(first or 1), int(last or first or 2**63)))
        sites = {
            key: site
            for key, site in sites.items()
            if any(
                site.chrom == chrom and start <= site.position <= end
                for chrom, start, end in intervals
            )
        }
    counts["reference_verified_normalized_probes"] = len(sites)
    with open(Path(workdir, "probe-map.tsv"), "w", newline="\n") as stream:
        stream.write(
            "#PROBE\tFORMAT\tID\tCHROM\tPOS\tCALL_CHROM\tCALL_POS\tREF\tALT\tREF_CODE\tALT_CODE\n"
        )
        for key, site in sites.items():
            stream.write(
                "\t".join(
                    (
                        key,
                        *metadata[key],
                        site.chrom,
                        str(site.position),
                        site.ref,
                        site.alt,
                        site.ref_code,
                        site.alt_code,
                    )
                )
                + "\n"
            )
    if not sites:
        Path(workdir, "indel-calls.tsv").write_text("#FORMAT\tID\tCHROM\tPOS\tCALL\n")
        _write_summary(workdir, counts, manifest_hashes, lib.build)
        return output
    targets = Path(workdir, "alleles.tsv")
    with targets.open("w", newline="\n") as stream:
        for site in sorted(set(sites.values()), key=lambda s: (s.chrom, s.position)):
            stream.write(f"{site.chrom}\t{site.position}\t{site.ref},{site.alt}\n")
    run_command(["bgzip", "-f", str(targets)])
    target_gz = str(targets) + ".gz"
    run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", target_gz])
    vcf = str(Path(workdir, "indels.vcf.gz"))
    is_vcf = args.input.endswith((".vcf", ".vcf.gz", ".bcf"))
    if is_vcf:
        chroms = vcf_index_chromosomes(args.input)
        if not any(match_chromosome_name(s.chrom, set(chroms)) for s in sites.values()):
            Path(workdir, "indel-calls.tsv").write_text(
                "#FORMAT\tID\tCHROM\tPOS\tCALL\n"
            )
            _write_summary(workdir, counts, manifest_hashes, lib.build)
            return output
        rename = Path(workdir, "rename-chromosomes.tsv")
        rename.write_text(
            "".join(
                f"{chrom}\t{matched}\n"
                for chrom in chroms
                if (
                    matched := match_chromosome_name(
                        chrom, {s.chrom for s in sites.values()}
                    )
                )
            )
        )
        regions = Path(workdir, "input-regions.tsv")
        source_sites = {
            key: replace(site, chrom=source_chrom)
            for key, site in sites.items()
            if (source_chrom := match_chromosome_name(site.chrom, set(chroms)))
        }
        _calling_regions(source_sites, regions, padding=200)
        run_pipeline(
            [
                ["bcftools", "view", "-R", str(regions), "-Ou", args.input],
                ["bcftools", "annotate", "--rename-chrs", str(rename), "-Ou"],
                ["bcftools", "norm", "-f", lib.fasta, "-Oz", "-o", vcf],
            ],
            stdout=None,
            description="Normalize manifest-defined input indels",
        )
    else:
        from wgsextract_cli.core import ultima

        calling_regions = Path(workdir, "calling-regions.tsv")
        _calling_regions(sites, calling_regions)
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
                    "10",
                    *ultima.bcftools_options(args.input)[0],
                    "-a",
                    "FORMAT/DP,FORMAT/AD",
                    "--threads",
                    threads,
                    "-f",
                    lib.fasta,
                    "-T",
                    target_gz,
                    "-R",
                    str(calling_regions),
                    args.input,
                ],
                [
                    "bcftools",
                    "call",
                    "-m",
                    "-C",
                    "alleles",
                    "-T",
                    target_gz,
                    "--keep-alts",
                    "-f",
                    "GQ",
                    *ploidy_args,
                    "-Oz",
                    "-o",
                    vcf,
                ],
            ],
            stdout=None,
            description="Call sequence-defined indel probes",
        )
    calls = _calls_from_vcf(
        vcf, sites, args, workdir, {key: data[1] for key, data in metadata.items()}
    )
    with open(Path(workdir, "indel-calls.tsv"), "w", newline="\n") as stream:
        stream.write("#FORMAT\tID\tCHROM\tPOS\tCALL\n")
        for key, result in calls.items():
            format_name, marker, chrom, position = metadata[key]
            profile = vendor_output_profile(format_name)
            if len(result) == 1:
                result *= 2
                if profile.haploid_style == "single":
                    result = _haploid_letter(
                        result, vendor_chrom(chrom), position, True
                    )
            output[format_name][marker, chrom, position] = result
            counts["called" if result != "--" else "rejected"] += 1
            stream.write(f"{format_name}\t{marker}\t{chrom}\t{position}\t{result}\n")
    _write_summary(workdir, counts, manifest_hashes, lib.build)
    return output
