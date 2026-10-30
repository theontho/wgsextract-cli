"""Allele-exact chip concordance, without exposing genotypes in reports."""

import csv
import gzip
from dataclasses import asdict, dataclass
from pathlib import Path

from wgsextract_cli.core.microarray_utils import (
    get_template_format,
    vendor_chrom,
    vendor_output_profile,
)

CONSUMER_FORMATS = (
    "23andMe_V2",
    "23andMe_V3",
    "23andMe_V4",
    "23andMe_V5",
    "23andMe_V5_2025",
    "23andMe_V35",
    "23andMe_SNPs_API",
    "Ancestry_V1",
    "Ancestry_V2",
    "Ancestry_V2_2021",
    "Ancestry_V2_2024",
    "FTDNA_V1_Affy",
    "FTDNA_V2",
    "FTDNA_V3",
    "MyHeritage_V1",
    "MyHeritage_V2",
    "MyHeritage_GSA_2025",
    "MyHeritage_GSA_2025_609424",
    "LDNA_V1",
    "LDNA_V2",
    "Nucleus_V1",
)


@dataclass(frozen=True)
class ChipCall:
    chromosome: str
    genotype: str | None
    conflict: bool = False


def _genotype(value: str) -> str | None:
    value = value.strip().upper()
    if "/" in value or "|" in value:
        separator = "/" if "/" in value else "|"
        alleles = value.split(separator)
        if len(alleles) != 2 or any(len(allele) != 1 for allele in alleles):
            return None
        value = "".join(alleles)
    return (
        "".join(sorted(value))
        if len(value) in {1, 2} and all(a in "ACGT" for a in value)
        else None
    )


def read_chip_export(path: Path, format_name: str) -> dict[str, ChipCall]:
    """Read an export or a four-column rsID/CHROM/POS/TGT chip query.

    Compare identities, not coordinates: exports can use different assemblies.
    No strand complementation, rsID remapping or indel interpretation is inferred.
    Conflicting duplicate identities remain quarantined, including no-call/call
    duplicates; a later row must never restore one.
    """
    if format_name != "CombinedKit" and get_template_format(format_name) is None:
        raise ValueError(f"Unknown chip format: {format_name}")
    profile = vendor_output_profile(format_name)
    delimiter = "," if profile.layout in {"csv", "quoted_csv"} else "\t"
    columns = 5 if profile.layout == "split_alleles" else 4
    calls: dict[str, ChipCall] = {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        for number, row in enumerate(csv.reader(stream, delimiter=delimiter), 1):
            if not row or row[0].startswith("#"):
                continue
            row = [value.strip() for value in row]
            if row[0].lower() in {"rsid", "snp", "snp_id"}:
                continue
            if len(row) != columns or not row[2].isdigit():
                raise ValueError(f"Invalid chip row at line {number}")
            chrom = vendor_chrom(row[1].removeprefix("chr").upper())
            if chrom == "M":
                chrom = "MT"
            if chrom not in {*map(str, range(23)), "X", "Y", "MT"}:
                raise ValueError(f"Invalid chip chromosome at line {number}")
            # Real FTDNA templates include unplaced (0/0) and position-0 probes.
            genotype = (
                _genotype("".join(row[3:]))
                if chrom != "0" and int(row[2]) > 0
                else None
            )
            call = ChipCall(chrom, genotype)
            previous = calls.get(row[0])
            if previous is not None and previous != call:
                call = ChipCall(previous.chromosome, None, conflict=True)
            calls[row[0]] = call
    return calls


@dataclass
class ConcordanceCounts:
    overlap: int = 0
    truth_nocall: int = 0
    chromosome_mismatch: int = 0
    eligible: int = 0
    called: int = 0
    concordant: int = 0
    discordant: int = 0
    nocall: int = 0

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            **asdict(self),
            "call_rate": self.called / self.eligible if self.eligible else None,
            "concordance": self.concordant / self.called if self.called else None,
        }

    def add(self, observed: ChipCall, truth: ChipCall) -> None:
        self.overlap += 1
        if truth.genotype is None:
            self.truth_nocall += 1
        elif observed.chromosome != truth.chromosome:
            self.chromosome_mismatch += 1
        else:
            self.eligible += 1
            if observed.genotype is None:
                self.nocall += 1
                return
            self.called += 1
            agrees = observed.genotype == truth.genotype
            # Vendor haploid conventions can differ on sex chromosomes and MT.
            if truth.chromosome in {"X", "Y", "MT"}:
                agrees |= observed.genotype * 2 == truth.genotype
                agrees |= truth.genotype * 2 == observed.genotype
            self.concordant += int(agrees)
            self.discordant += int(not agrees)


@dataclass
class ChipConcordance:
    truth_markers: int
    kit_markers: int
    total: ConcordanceCounts
    chromosomes: dict[str, ConcordanceCounts]

    def as_dict(self) -> dict[str, object]:
        return {
            "truth_markers": self.truth_markers,
            "kit_markers": self.kit_markers,
            "total": self.total.as_dict(),
            "chromosomes": {
                chrom: counts.as_dict() for chrom, counts in self.chromosomes.items()
            },
        }


def score_chip(kit: dict[str, ChipCall], truth: dict[str, ChipCall]) -> ChipConcordance:
    total = ConcordanceCounts()
    chromosomes: dict[str, ConcordanceCounts] = {}
    for marker, observed in kit.items():
        expected = truth.get(marker)
        if expected is None:
            continue
        total.add(observed, expected)
        chromosomes.setdefault(expected.chromosome, ConcordanceCounts()).add(
            observed, expected
        )
    return ChipConcordance(len(truth), len(kit), total, chromosomes)
