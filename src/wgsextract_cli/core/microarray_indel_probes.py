"""Sequence-defined indel probes, never inferred from an I/D genotype or rsID."""

import contextlib
import csv
import gzip
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from wgsextract_cli.core.microarray_utils import vendor_chrom
from wgsextract_cli.core.utils import WGSExtractError

_CONTEXT = re.compile(r"^([ACGT]{15,})\[([ACGT]+|-)/([ACGT]+|-)\]([ACGT]{15,})$")
_COMPLEMENT = str.maketrans("ACGT", "TGCA")
_MAX_MANIFEST_BYTES = 1024**3


@dataclass(frozen=True)
class IndelProbe:
    marker: str
    chrom: str
    position: int
    context: str


@dataclass(frozen=True)
class IndelSite:
    chrom: str
    position: int
    ref: str
    alt: str
    ref_code: str

    @property
    def alt_code(self) -> str:
        return "D" if self.ref_code == "I" else "I"


@contextlib.contextmanager
def _manifest_stream(path: str) -> Iterator[TextIO]:
    with contextlib.ExitStack() as stack:
        stream: TextIO
        if path.lower().endswith(".zip"):
            archive = stack.enter_context(zipfile.ZipFile(path))
            members = [
                member
                for member in archive.infolist()
                if member.filename.lower().endswith(".csv")
            ]
            if len(members) != 1:
                raise WGSExtractError(
                    "An indel manifest ZIP must contain exactly one CSV."
                )
            if members[0].file_size > _MAX_MANIFEST_BYTES:
                raise WGSExtractError("Indel manifest CSV exceeds the 1 GiB limit.")
            raw = stack.enter_context(archive.open(members[0]))
            stream = stack.enter_context(
                io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            )
        elif path.lower().endswith(".gz"):
            stream = stack.enter_context(
                gzip.open(path, "rt", encoding="utf-8-sig", newline="")
            )
        else:
            stream = stack.enter_context(open(path, encoding="utf-8-sig", newline=""))
        yield stream


def read_indel_manifest(
    path: str, *, indel_ids: set[str] | None = None
) -> dict[tuple[str, str, int], IndelProbe]:
    """Read an Illumina CSV's build-37 I/D sequence definitions.

    Conflicting definitions at the same marker/coordinate are quarantined,
    irrespective of row order. No strand label is trusted without matching
    the complete flanking sequence to the selected reference.
    """
    if not Path(path).is_file():
        raise WGSExtractError(f"Indel manifest not found: {path}")
    required = {"Name", "SNP", "GenomeBuild", "Chr", "MapInfo", "SourceSeq"}
    probes: dict[tuple[str, str, int], IndelProbe] = {}
    rejected: set[tuple[str, str, int]] = set()
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
            if row.get("SNP") not in {"[I/D]", "[D/I]"}:
                continue
            if indel_ids is not None and row.get("Name"):
                indel_ids.add(row["Name"])
            if row.get("GenomeBuild") not in {"37", "GRCh37", "hg19"}:
                raise WGSExtractError(
                    "Indel probe manifests must use GRCh37 coordinates."
                )
            chrom, position = vendor_chrom(row.get("Chr", "")), row.get("MapInfo", "")
            context, marker = row.get("SourceSeq", "").upper(), row.get("Name", "")
            if (
                not marker
                or not position.isdigit()
                or int(position) < 1
                or not _CONTEXT.fullmatch(context)
                or chrom not in {*map(str, range(1, 23)), "X", "Y", "MT"}
            ):
                continue
            match = _CONTEXT.fullmatch(context)
            assert match is not None
            _, a, b, _ = match.groups()
            a, b = a.replace("-", ""), b.replace("-", "")
            if len(a) == len(b):
                continue
            while a and b and a[0] == b[0]:
                a, b = a[1:], b[1:]
            while a and b and a[-1] == b[-1]:
                a, b = a[:-1], b[:-1]
            if a and b:
                continue
            key = marker, chrom, int(position)
            probe = IndelProbe(marker, chrom, int(position), context)
            if key in rejected:
                continue
            if key in probes and probes[key] != probe:
                rejected.add(key)
                probes.pop(key)
            else:
                probes[key] = probe
    if not probes:
        raise WGSExtractError("No unambiguous sequence-defined I/D probes in manifest.")
    return probes


def match_indel_reference(
    probe: IndelProbe, chrom: str, window_start: int, sequence: str
) -> IndelSite | None:
    """Find exactly one reference haplotype from both fully flanked alleles."""
    match = _CONTEXT.fullmatch(probe.context)
    if not match:
        return None
    left, first, second, right = match.groups()
    alleles = first.replace("-", ""), second.replace("-", "")
    sequence = sequence.upper()
    candidates: set[IndelSite] = set()
    for reverse in (False, True):
        flank_left, flank_right = left, right
        choices = list(alleles)
        if reverse:
            flank_left = right.translate(_COMPLEMENT)[::-1]
            flank_right = left.translate(_COMPLEMENT)[::-1]
            choices = [a.translate(_COMPLEMENT)[::-1] for a in alleles]
        for index, allele in enumerate(choices):
            haplotype = flank_left + allele + flank_right
            offset = sequence.find(haplotype)
            while offset >= 0:
                position = window_start + offset + len(flank_left) - 1
                anchor = flank_left[-1]
                candidates.add(
                    IndelSite(
                        chrom,
                        position,
                        anchor + allele,
                        anchor + choices[1 - index],
                        "I" if len(allele) > len(choices[1 - index]) else "D",
                    )
                )
                offset = sequence.find(haplotype, offset + 1)
    return next(iter(candidates)) if len(candidates) == 1 else None
