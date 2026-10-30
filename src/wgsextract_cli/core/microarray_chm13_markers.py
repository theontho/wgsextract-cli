"""Conservative known-marker exclusions for the SNP-only CHM13 caller."""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping

from wgsextract_cli.core.microarray_indel_probes import _manifest_stream
from wgsextract_cli.core.microarray_utils import (
    _resolve_templates_for_format,
    get_template_format,
    template_rows,
    vendor_chrom,
)
from wgsextract_cli.core.utils import WGSExtractError


def non_snv_alleles(ref: str, alt: str) -> bool:
    return any(
        len(allele) != 1 or allele.upper() not in "ACGT" for allele in (ref, alt)
    )


def manifest_exclusions(
    bindings: Mapping[str, str],
) -> tuple[set[str], dict[tuple[str, int], str]]:
    ids: set[str] = set()
    sites: dict[tuple[str, int], str] = {}
    required = {"Name", "SNP", "GenomeBuild", "Chr", "MapInfo", "SourceSeq"}
    for path in bindings.values():
        with _manifest_stream(path) as stream:
            reader = csv.reader(stream)
            for header in reader:
                if required <= set(header):
                    break
            else:
                raise WGSExtractError(
                    "Indel manifest lacks Illumina probe/sequence columns."
                )
            for fields in reader:
                if fields and fields[0] == "[Controls]":
                    break
                row = dict(zip(header, fields, strict=False))
                alleles = re.findall(
                    r"\[([^/\]]+)/([^/\]]+)\]",
                    row.get("SNP", "") + " " + row.get("SourceSeq", ""),
                )
                if not any(non_snv_alleles(a.upper(), b.upper()) for a, b in alleles):
                    continue
                if row.get("GenomeBuild") not in {"37", "GRCh37", "hg19"}:
                    raise WGSExtractError(
                        "Indel probe manifests must use GRCh37 coordinates."
                    )
                marker = row.get("Name", "")
                if marker:
                    ids.add(marker)
                chrom = vendor_chrom(row.get("Chr", ""))
                position = row.get("MapInfo", "")
                if (
                    chrom in {*map(str, range(1, 23)), "X", "Y", "MT"}
                    and position.isdigit()
                    and int(position) > 0
                ):
                    sites[
                        ("chrM" if chrom == "MT" else "chr" + chrom, int(position))
                    ] = "unsupported_indel_marker"
    return ids, sites


def vendor_no_calls(
    format_name: str, roots: list[str], rejected_ids: set[str]
) -> dict[tuple[str, str, str], str]:
    if not rejected_ids:
        return {}
    info = get_template_format(format_name)
    if info is None:
        return {}
    _, bodies = _resolve_templates_for_format(roots, format_name, info)
    return {
        (marker, chrom, pos): "--"
        for path in bodies
        for marker, chrom, pos in template_rows(format_name, path)
        if marker in rejected_ids
    }
