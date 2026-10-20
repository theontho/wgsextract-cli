"""`typing str`: repeat expansions with ExpansionHunter.

ExpansionHunter (Illumina; Apache-2.0) sizes the 31 disease-causing short
tandem repeats of its shipped catalog (HTT, FMR1, C9ORF72, ...) from a WGS
BAM/CRAM, including expansions longer than a read. Each allele is then put in
STRchive's normal, intermediate or pathogenic range for its disease.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from dataclasses import dataclass

from wgsextract_cli.commands.info.metrics import sample_sex
from wgsextract_cli.core.dependencies import pixi_env_prefix
from wgsextract_cli.core.utils import WGSExtractError, run_command

from .common import Alignment, env_tool, open_alignment


@dataclass(frozen=True)
class Threshold:
    disease: str
    inheritance: str  # AD, AR, XD, XR, or AD/AR
    benign_max: int | None  # None: no intermediate range
    pathogenic_min: int
    premutation_min: int | None = None


# Repeat counts from STRchive (https://strchive.org, MIT license), keyed by the
# catalog's VariantId for each pathogenic repeat. PABPN1 is in the catalog's
# units, the (GCG) tract before (GCA)3GCG: STRchive's GCN counts minus 4.
# DIP2B and NIPA1 are left out: their disease association is disputed.
THRESHOLDS = {
    "AFF2": Threshold("FRAXE", "XR", 39, 201),
    "AR": Threshold("SBMA", "XR", 34, 38),
    "ATN1": Threshold("DRPLA", "AD", 35, 48),
    "ATXN1": Threshold("SCA1", "AD", 35, 39),
    "ATXN10": Threshold("SCA10", "AD", 32, 800),
    "ATXN2": Threshold("SCA2", "AD/AR", 28, 35),
    "ATXN3": Threshold("SCA3", "AD", 44, 60),
    "ATXN7": Threshold("SCA7", "AD", 27, 37),
    "ATXN8OS": Threshold("SCA8", "AD", 50, 71),
    "C9ORF72": Threshold("FTD/ALS", "AD", 23, 31),
    "CACNA1A": Threshold("SCA6", "AD", 18, 21),
    "CBL": Threshold("Jacobsen syndrome", "AD", 79, 101),
    "CNBP": Threshold("DM2", "AD", 26, 75),
    "CSTB": Threshold("EPM1", "AR", 3, 30),
    "DMPK": Threshold("DM1", "AD", 34, 50),
    "FMR1": Threshold("Fragile X", "XD", 44, 201, premutation_min=55),
    "FXN": Threshold("Friedreich ataxia", "AR", 33, 56),
    "GIPC1": Threshold("OPDM2", "AD", 32, 73),
    "GLS": Threshold("GDPAG", "AR", 38, 680),
    "HTT": Threshold("Huntington disease", "AD", 26, 36),
    "JPH3": Threshold("HDL2", "AD", 28, 40),
    "NOP56": Threshold("SCA36", "AD", 14, 650),
    "NOTCH2NL": Threshold("NIID", "AD", 37, 66),
    "PABPN1": Threshold("OPMD", "AD/AR", 6, 8),
    "PHOX2B": Threshold("CCHS", "AD", 20, 26),
    "PPP2R2B": Threshold("SCA12", "AD", 32, 51),
    # Pathogenic RFC1 expansions are AAGGG; the benign AAAAG and AAAGG ones
    # reach the same lengths, and ExpansionHunter's count doesn't tell them
    # apart, so only the length is reported, without an intermediate range.
    "RFC1": Threshold("CANVAS", "AR", None, 400),
    "TBP": Threshold("SCA17", "AD", 40, 49),
    "TCF4": Threshold("FECD3", "AD", 39, 51),
}
MOTIF_UNCHECKED = {"RFC1"}


@dataclass(frozen=True)
class RepeatCall:
    variant_id: str
    repeat_unit: str
    region: str
    alleles: tuple[int, ...]
    intervals: tuple[tuple[int, int], ...]
    coverage: float


def parse_expansionhunter(json_path: str) -> list[RepeatCall]:
    with open(json_path) as f:
        loci = json.load(f).get("LocusResults", {})
    calls = []
    for locus in loci.values():
        for variant_id, variant in (locus.get("Variants") or {}).items():
            genotype = variant.get("Genotype")
            if not genotype:
                continue
            intervals = [
                tuple(int(bound) for bound in ci.split("-"))
                for ci in variant["GenotypeConfidenceInterval"].split("/")
            ]
            calls.append(
                RepeatCall(
                    variant_id=variant_id,
                    repeat_unit=variant.get("RepeatUnit", ""),
                    region=variant.get("ReferenceRegion", ""),
                    alleles=tuple(int(a) for a in genotype.split("/")),
                    intervals=tuple((low, high) for low, high in intervals),
                    coverage=float(locus.get("Coverage") or 0),
                )
            )
    return calls


def classify(call: RepeatCall, threshold: Threshold | None) -> str:
    """normal, intermediate, premutation, carrier or pathogenic (or "-" for a
    repeat without thresholds), with a note when an allele's confidence
    interval reaches the pathogenic range but its call doesn't."""
    if threshold is None:
        return "-"
    expanded = sum(a >= threshold.pathogenic_min for a in call.alleles)
    recessive = threshold.inheritance == "AR" or (
        threshold.inheritance == "XR" and len(call.alleles) > 1
    )
    if expanded and recessive and expanded < len(call.alleles):
        status = "carrier"
    elif expanded:
        status = "pathogenic"
    elif threshold.premutation_min is not None and any(
        a >= threshold.premutation_min for a in call.alleles
    ):
        status = "premutation"
    elif threshold.benign_max is not None and any(
        a > threshold.benign_max for a in call.alleles
    ):
        status = "intermediate"
    else:
        status = "normal"
    if status != "pathogenic" and any(
        allele < threshold.pathogenic_min <= high
        for allele, (_low, high) in zip(call.alleles, call.intervals, strict=True)
    ):
        status += " (CI reaches pathogenic)"
    return status


def _catalog(aln: Alignment, catalog: str | None) -> str:
    if catalog:
        if not os.path.isfile(catalog):
            raise WGSExtractError(f"Variant catalog not found: {catalog}")
        return catalog
    prefix = pixi_env_prefix("expansionhunter")
    name = {
        ("hg38", True): "hg38",
        ("hg38", False): "grch38",
        ("hg19", True): "hg19",
        ("hg19", False): "grch37",
    }[(aln.build, aln.chr_prefix)]
    path = (
        prefix / "share/ExpansionHunter/variant_catalog" / name / "variant_catalog.json"
        if prefix
        else None
    )
    if not (path and path.is_file()):
        raise WGSExtractError(
            "ExpansionHunter's variant catalog wasn't found (pixi install -e "
            "expansionhunter), or pass --catalog."
        )
    return str(path)


def _sex(aln: Alignment, sex: str | None) -> str:
    if sex:
        return sex
    inferred = sample_sex(aln.path, aln.outdir).lower()
    if inferred in ("male", "female"):
        logging.info(f"Sample sex: {inferred} (from chrX/chrY coverage)")
        return inferred
    logging.warning(
        "Couldn't infer the sample's sex; treating it as female (two X "
        "alleles). Pass --sex if that's wrong."
    )
    return "female"


def _format_alleles(call: RepeatCall) -> tuple[str, str]:
    return (
        "/".join(map(str, call.alleles)),
        "/".join(f"{low}-{high}" for low, high in call.intervals),
    )


def write_summary(path: str, calls: list[RepeatCall]) -> list[tuple[str, str]]:
    """A TSV of each pathogenic repeat's call and range; returns the
    (locus, line) of every one that isn't normal."""
    flagged = []
    with open(path, "w") as f:
        f.write(
            "locus\tdisease\tinheritance\trepeat_unit\tgenotype\tci\t"
            "normal_max\tpathogenic_min\tstatus\tcoverage\n"
        )
        for call in sorted(calls, key=lambda c: c.variant_id):
            threshold = THRESHOLDS.get(call.variant_id)
            known = threshold or Threshold("-", "-", 0, 0)
            if threshold is None and "_" in call.variant_id:
                continue  # A flanking repeat of a catalog locus.
            status = classify(call, threshold)
            genotype, ci = _format_alleles(call)
            f.write(
                "\t".join(
                    [
                        call.variant_id,
                        known.disease,
                        known.inheritance,
                        call.repeat_unit,
                        genotype,
                        ci,
                        str(known.benign_max)
                        if threshold and known.benign_max is not None
                        else "-",
                        str(known.pathogenic_min) if threshold else "-",
                        status,
                        f"{call.coverage:.1f}",
                    ]
                )
                + "\n"
            )
            if status not in ("normal", "-"):
                note = (
                    " (motif not checked)" if call.variant_id in MOTIF_UNCHECKED else ""
                )
                flagged.append(
                    (
                        call.variant_id,
                        f"{call.variant_id} ({known.disease}, {known.inheritance}): "
                        f"{call.repeat_unit} {genotype} [CI {ci}] {status}{note}",
                    )
                )
    return flagged


def cmd_str(args: argparse.Namespace) -> None:
    aln = open_alignment(args)
    reference = aln.require_reference()
    expansion_hunter = env_tool("ExpansionHunter", "expansionhunter")
    catalog = _catalog(aln, args.catalog)
    sex = _sex(aln, args.sex)

    prefix = os.path.join(aln.outdir, f"{aln.stem}.str")
    logging.info("Sizing repeat expansions with ExpansionHunter...")
    try:
        run_command(
            [*expansion_hunter, "--reads", aln.path, "--reference", reference,
             "--variant-catalog", catalog, "--output-prefix", prefix,
             "--sex", sex, "--threads", aln.threads],
            capture_output=not getattr(args, "debug", False),
        )  # fmt: skip
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip().splitlines()[-1:] if e.stderr else []
        raise WGSExtractError(
            f"ExpansionHunter failed (exit {e.returncode})"
            + (f": {detail[0]}" if detail else ".")
        ) from e

    calls = parse_expansionhunter(f"{prefix}.json")
    summary = f"{prefix}.tsv"
    flagged = write_summary(summary, calls)
    classified = sum(1 for c in calls if c.variant_id in THRESHOLDS)
    logging.info(
        f"{classified - len(flagged)} of {classified} disease repeats are in the "
        "normal range."
    )
    for _locus, line in flagged:
        logging.warning(f"  {line}")
    if flagged:
        logging.warning(
            "Short-read sizing is approximate and isn't a diagnosis; confirm "
            "anything outside the normal range with a clinical test."
        )
    logging.info(
        f"Repeat calls: {summary} (ExpansionHunter: {prefix}.vcf, {prefix}.json)"
    )
