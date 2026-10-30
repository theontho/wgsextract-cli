"""CHM13-only calling; the existing GRCh37/38 route remains independent."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.microarray_aadr import (
    _chrom_name,
    aadr_markers,
    aadr_panel,
    write_aadr_outputs,
)
from wgsextract_cli.core.microarray_chm13 import (
    CHM13Catalogue,
    load_chm13_catalogue,
    validate_chm13_alignment_header,
    validate_chm13_reference,
    write_chm13_ploidy,
)
from wgsextract_cli.core.microarray_chm13_calls import (
    canonical_chromosome,
    parse_native_calls,
    project_native_calls,
)
from wgsextract_cli.core.microarray_identity import identity_policy
from wgsextract_cli.core.microarray_quality import quality_policy
from wgsextract_cli.core.microarray_site_map import SiteProjection
from wgsextract_cli.core.microarray_targets import collect_template_markers
from wgsextract_cli.core.microarray_utils import (
    convert_to_vendor_format,
    get_template_format,
)
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.sample_ploidy import sex_samples_args
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import chromosome_aliases

from ._microarray_combined import _command_lines, _reference_alleles
from ._microarray_vcf import _prepare_microarray_vcf


def _regions(region: str | None) -> list[tuple[str, int, int]]:
    result = []
    try:
        for item in region.split(",") if region else []:
            chrom, _, span = item.partition(":")
            bounds = span.replace(",", "").split("-")
            start = int(bounds[0]) if bounds[0] else 1
            end = (
                (int(bounds[-1]) if bounds[-1] else 0xFFFFFFFF)
                if len(bounds) == 2
                else (start if span else 0xFFFFFFFF)
            )
            if not 1 <= start <= end <= 0xFFFFFFFF or len(bounds) > 2:
                raise ValueError("invalid interval")
            result.append((canonical_chromosome(chrom), start, end))
    except ValueError as error:
        raise WGSExtractError(f"Invalid CHM13 region: {region}") from error
    return result


def _selected(site: SiteProjection, regions: list[tuple[str, int, int]]) -> bool:
    if site.source_chrom == "chrM" and site.status == "mt_gap":
        return not regions or any(
            chrom == "chrM" and start == 1 and end >= 16569
            for chrom, start, end in regions
        )
    return not regions or any(
        site.target_chrom == chrom
        and site.target_pos is not None
        and start <= site.target_pos <= end
        for chrom, start, end in regions
    )


def _marker_ids(
    catalogue: CHM13Catalogue, roots: list[str]
) -> dict[tuple[str, int], str]:
    chain = next(
        (
            Path(root, "hg18ToHg19.over.chain.gz")
            for root in roots
            if Path(root, "hg18ToHg19.over.chain.gz").is_file()
        ),
        None,
    )
    if chain is None:
        raise WGSExtractError(
            "CHM13 microarray needs the installed hg18ToHg19.over.chain.gz "
            "and the complete template set used by its pinned site map."
        )
    markers = {
        (chrom, pos): marker
        for (chrom, pos), marker in collect_template_markers(roots, str(chain)).items()
        if chrom in catalogue.source_lengths and pos <= catalogue.source_lengths[chrom]
    }
    for marker in aadr_markers("1240k"):
        chrom = canonical_chromosome(_chrom_name(marker.chrom))
        if chrom in catalogue.source_lengths and (
            1 <= marker.position <= catalogue.source_lengths[chrom]
        ):
            markers.setdefault((chrom, marker.position), marker.snp_id)
    count = 0
    for site in catalogue.mapping.source_sites():
        count += 1
        if (site.source_chrom, site.source_pos) not in markers:
            raise WGSExtractError(
                "Installed templates do not cover the pinned CHM13 map."
            )
    if len(markers) != count:
        raise WGSExtractError(
            "Installed templates differ from the pinned CHM13 map union."
        )
    return markers


def _target(
    catalogue: CHM13Catalogue,
    marker_ids: dict[tuple[str, int], str],
    names: dict[str, str],
    regions: list[tuple[str, int, int]],
    directory: Path,
) -> str:
    plain = directory / "native-targets.tab"
    count = 0
    with plain.open("w", newline="\n") as destination:
        destination.write("##wgsextract_chm13_site_map=1\n#CHROM\tPOS\tID\n")
        for site in catalogue.mapping.native_sites():
            if not _selected(site, regions):
                continue
            assert site.target_chrom is not None
            marker = marker_ids[(site.source_chrom, site.source_pos)]
            destination.write(
                f"{names[site.target_chrom]}\t{site.target_pos}\t{marker}\n"
            )
            count += 1
    if not count:
        raise WGSExtractError(
            "No supported CHM13 microarray sites in the selected region."
        )
    run_command(["bgzip", "-f", str(plain)])
    target = str(plain) + ".gz"
    run_command(["tabix", "-s", "1", "-b", "2", "-e", "2", target])
    return target


def _extract_vcf(
    source: str,
    target: str,
    output: str,
    catalogue: CHM13Catalogue,
    names: dict[str, str],
    directory: Path,
) -> None:
    seen: set[str] = set()
    for line in _command_lines(["bcftools", "view", "-h", source], str(directory)):
        match = re.match(r"##contig=<ID=([^,>]+)", line)
        if not match:
            continue
        actual = match[1]
        chrom = canonical_chromosome(actual)
        if chrom not in catalogue.contigs:
            continue
        if chrom in seen:
            raise WGSExtractError("Duplicate chromosome aliases in native VCF header.")
        seen.add(chrom)
        length = re.search(r"[,<]length=([0-9]+)", line)
        md5 = re.search(r"[,<](?:md5|MD5)=([0-9a-fA-F]+)", line)
        if (
            length
            and int(length[1]) != catalogue.contigs[chrom].length
            or md5
            and md5[1].lower() != catalogue.contigs[chrom].md5
        ):
            raise WGSExtractError(f"Native VCF reference signature mismatch: {actual}")
    if not seen:
        raise WGSExtractError(
            "Native VCF header must declare its primary chromosomes in ##contig rows."
        )
    rename = directory / "vcf-chromosome-names.tsv"
    rename.write_text(
        "".join(
            f"{alias}\t{actual}\n"
            for chrom, actual in names.items()
            for alias in chromosome_aliases(chrom)
            if alias != actual
        ),
        newline="\n",
    )
    run_pipeline(
        [
            ["bcftools", "annotate", "--rename-chrs", str(rename), "-Ou", source],
            [
                "bcftools",
                "view",
                "--targets-file",
                target,
                "--targets-overlap",
                "1",
                "-Oz",
                "-o",
                output,
            ],
        ],
        stdout=subprocess.DEVNULL,
        description="Native CHM13 VCF extraction",
    )


def run_chm13(
    args: argparse.Namespace,
    lib: ReferenceLibrary,
    ref_fasta: str,
    outdir: str,
    threads: str,
) -> None:
    from .microarray import _resolve_microarray_format, _template_search_roots

    if args.ref_vcf_tab or args.ploidy_file:
        raise WGSExtractError(
            "CHM13 uses its pinned site map and validated PAR ploidy; "
            "--ref-vcf-tab and --ploidy-file overrides are not supported."
        )
    catalogue = load_chm13_catalogue()
    names = validate_chm13_reference(Path(ref_fasta), catalogue)
    regions = _regions(args.region)
    if any(chrom not in names for chrom, _start, _end in regions):
        raise WGSExtractError("CHM13 region names an unsupported chromosome.")
    is_vcf = args.input.endswith((".vcf", ".vcf.gz", ".vcf.bgz", ".bcf"))
    input_chroms = None
    if not is_vcf:
        input_chroms = validate_chm13_alignment_header(
            get_bam_header(args.input, ref_fasta), catalogue, names
        )
    roots = _template_search_roots(ref_fasta, lib)
    markers = _marker_ids(catalogue, roots)
    quality, identity = quality_policy(args), identity_policy(args)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="CHM13_", dir=outdir))
    target = _target(catalogue, markers, names, regions, directory)
    base = input_stem(args.input)
    out_vcf = str(directory / f"{base}_native.vcf.gz")
    if is_vcf:
        _extract_vcf(args.input, target, out_vcf, catalogue, names, directory)
    else:
        ploidy = write_chm13_ploidy(catalogue, names, directory)
        region_args = (
            [
                "-r",
                ",".join(
                    f"{names[chrom]}:{start}-{min(end, catalogue.contigs[chrom].length)}"
                    for chrom, start, end in regions
                ),
            ]
            if regions
            else []
        )
        ploidy_args = ["--ploidy-file", str(ploidy)]
        with sex_samples_args(
            args.input,
            str(directory),
            ploidy_args,
            region=region_args[1] if region_args else None,
        ) as sex_args:
            out_vcf, target = _prepare_microarray_vcf(
                args=args,
                outdir=str(directory),
                base_name=base,
                is_vcf=False,
                ref_vcf_tab=target,
                region_args=region_args,
                ploidy_args=ploidy_args + sex_args,
                ref_fasta=ref_fasta,
                threads=threads,
                start_vcf=time.time(),
                out_vcf=out_vcf,
                input_chroms=input_chroms,
            )
    samples = list(_command_lines(["bcftools", "query", "-l", out_vcf], str(directory)))
    if len(samples) != 1:
        raise WGSExtractError("CHM13 microarray requires exactly one input sample.")
    # REF validation precedes projection; reference differences between builds
    # are diagnostics, but a record disagreeing with its actual reference is not.
    run_command(
        ["bcftools", "norm", "--check-ref", "e", "-f", ref_fasta, "-Ou", out_vcf],
        stdout=subprocess.DEVNULL,
    )
    calls, represented = parse_native_calls(
        _command_lines(
            [
                "bcftools",
                "query",
                "-u",
                "-f",
                "%CHROM\t%POS\t%END\t%REF\t%ALT\t[%GT\t%DP\t%GQ]\t%FILTER\n",
                out_vcf,
            ],
            str(directory),
        ),
        catalogue.mapping,
        markers,
        quality,
        identity,
    )
    reference = {}
    if is_vcf and quality.assume_hom_ref and not (quality.min_dp or quality.min_gq):
        reference = {
            (canonical_chromosome(chrom), int(pos)): base
            for (chrom, pos), base in _reference_alleles(
                ref_fasta, target, str(directory)
            ).items()
        }
    combined = directory / f"{base}_CombinedKit.txt"
    counts: Counter[str] = Counter()
    ref_counts: Counter[str] = Counter()
    with contextlib.ExitStack() as stack:
        destination = stack.enter_context(combined.open("w", newline="\n"))
        destination.write("# RSID\tCHROM\tPOS\tRESULT\n")
        reasons = (
            stack.enter_context(
                combined.with_name(combined.stem + "_reasons.tsv").open(
                    "w", newline="\n"
                )
            )
            if quality.call_reasons
            else None
        )
        if reasons:
            reasons.write("# RSID\tCHROM\tPOS\tRESULT\tCALL_REASON\tREF_DIAGNOSTIC\n")
        sites: Iterator[SiteProjection] = (
            site
            for site in catalogue.mapping.source_sites()
            if _selected(site, regions)
        )
        for call in project_native_calls(
            sites,
            markers,
            calls,
            quality=quality,
            identity=identity,
            is_vcf=is_vcf,
            represented=represented,
            native_bases=reference,
        ):
            chrom = (
                "MT" if call.site.source_chrom == "chrM" else call.site.source_chrom[3:]
            )
            row = f"{call.snp_id}\t{chrom}\t{call.site.source_pos}\t{call.genotype}"
            destination.write(row + "\n")
            if reasons:
                reasons.write(f"{row}\t{call.reason}\t{call.site.ref_status}\n")
            counts[call.reason] += 1
            ref_counts[call.site.ref_status] += 1
    del calls, markers, reference
    for key in dict.fromkeys(args.formats.split(",")):
        panel = aadr_panel(key)
        if panel:
            write_aadr_outputs(
                panel,
                str(combined),
                str(Path(outdir, f"{base}_AADR_{panel}")),
                sample_id=getattr(args, "aadr_sample_id", "sample"),
                sex=getattr(args, "aadr_sex", "U"),
            )
            continue
        real_format = _resolve_microarray_format(key)
        if real_format is None:
            continue
        info = get_template_format(real_format)
        suffix = info["suffix"] if info else ".txt"
        chain36 = next(
            (
                str(Path(root, "hg18ToHg19.over.chain.gz"))
                for root in roots
                if Path(root, "hg18ToHg19.over.chain.gz").is_file()
            ),
            None,
        )
        convert_to_vendor_format(
            real_format,
            str(combined),
            str(Path(outdir, f"{base}_{real_format}{suffix}")),
            roots,
            chain36,
            identity=identity,
        )
    report = Path(outdir, f"{base}_CHM13_report.json")
    report.write_text(
        json.dumps(
            {
                "source_build": "CHM13v2.0-originalMT-maskedY",
                "output_coordinates": "GRCh37-rCRS",
                "call_reasons": dict(counts),
                "reference_diagnostics": dict(ref_counts),
                "probe_types": "Marker-only vendor IDs are not certified SNP probe types",
                "combined_kit": str(combined),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        newline="\n",
    )
    logging.info("CHM13 CombinedKit: %s; diagnostics: %s", combined, report)
